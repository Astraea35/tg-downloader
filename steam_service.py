#!/usr/bin/env python3
"""
Steam Workshop Helper Microservice for Wallpaper Engine Downloader
Runs on FnOS host (port 8192) to interface with SteamCMD container,
manage workshop files, and track real-time download progress and sizes.
Features a sequential download queue to eliminate SteamCMD concurrency conflicts.
"""

import glob
import http.client
import http.server
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "0.0.0.0"
PORT = 8192

STEAMCMD_DATA_DIR = "/vol1/1000/Docker/tg-downloader/steamcmd_data"
STEAM_CONFIG_FILE = "/vol1/1000/Docker/tg-downloader/steamcmd_config.json"
DOWNLOADS_BASE = "/vol1/1000/Docker/tg-downloader/downloads/wallpaper_engine"

DOWNLOAD_QUEUE = queue.Queue()
ACTIVE_TASKS = {}
TASKS_LOCK = threading.Lock()


def format_size(b: float) -> str:
  if b < 1024:
    return f"{b:.1f} B"
  elif b < 1024 * 1024:
    return f"{b / 1024:.1f} KB"
  elif b < 1024 * 1024 * 1024:
    return f"{b / (1024 * 1024):.1f} MB"
  else:
    return f"{b / (1024 * 1024 * 1024):.2f} GB"


def get_dir_size(path: str) -> int:
  if not os.path.exists(path):
    return 0
  total = 0
  try:
    with os.scandir(path) as it:
      for entry in it:
        if entry.is_file(follow_symlinks=False):
          total += entry.stat().st_size
        elif entry.is_dir(follow_symlinks=False):
          total += get_dir_size(entry.path)
  except Exception:
    pass
  return total


def load_config():
  if os.path.exists(STEAM_CONFIG_FILE):
    try:
      with open(STEAM_CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
    except Exception:
      pass
  return {"current_user": None, "logged_in": False}


def save_config(cfg):
  with open(STEAM_CONFIG_FILE, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)


def run_steamcmd(args: list, timeout: int = 120) -> tuple[int, str]:
  proxy = os.environ.get("STEAM_PROXY") or load_config().get("steam_proxy")
  proxy_args = [
      entry for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
      for entry in ("--env", f"{name}={proxy}")
  ] if proxy else []
  cmd = [
      "docker",
      "run",
      "--rm",
      *proxy_args,
      "-v",
      f"{STEAMCMD_DATA_DIR}/root:/root",
      "-v",
      f"{STEAMCMD_DATA_DIR}/Steam:/root/.local/share/Steam",
      "steamcmd/steamcmd:latest",
  ] + args

  try:
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    return p.returncode, p.stdout
  except subprocess.TimeoutExpired:
    return -1, "SteamCMD 操作超时"
  except Exception as e:
    return -2, f"运行 SteamCMD 异常: {e}"


def process_item_download(item_id: str, title: str, total_bytes: int):
  """Sequentially downloads or copies a workshop item cleanly."""
  cfg = load_config()
  login_user = cfg.get("current_user")
  if not login_user or not cfg.get("logged_in"):
    with TASKS_LOCK:
      ACTIVE_TASKS[item_id] = {
          "status": "not_logged_in",
          "item_id": item_id,
          "message": (
              "尚未登录 Steam 账号！由于小红车为付费软件，请先使用 /steam_login"
              " 登录拥有小红车的 Steam 账号。"
          ),
      }
    return

  # 1. Check if destination already has valid files (> 1MB)
  matching_dirs = glob.glob(os.path.join(DOWNLOADS_BASE, f"{item_id}*"))
  if not matching_dirs:
    # Also check legacy date subdirectories just in case
    matching_dirs = glob.glob(os.path.join(DOWNLOADS_BASE, "*", f"{item_id}*"))
  for md in matching_dirs:
    if os.path.isdir(md):
      sz = get_dir_size(md)
      if sz > 1024 * 1024:
        copied_files = []
        video_file = None
        preview_file = None
        real_title = title
        wallpaper_type = "scene"
        for root, dirs, files in os.walk(md):
          for f in files:
            dst_file = os.path.join(root, f)
            rel_path = os.path.relpath(dst_file, md)
            copied_files.append(rel_path)
            lower_f = f.lower()
            if lower_f.endswith((".mp4", ".webm", ".mkv")):
              video_file = dst_file
            elif lower_f.startswith("preview") and lower_f.endswith(
                (".gif", ".jpg", ".jpeg", ".png")
            ):
              preview_file = dst_file
            if f == "project.json":
              try:
                with open(
                    dst_file, "r", encoding="utf-8", errors="ignore"
                ) as pf:
                  pdata = json.load(pf)
                  if pdata.get("title"):
                    real_title = pdata.get("title").strip()
                  if pdata.get("type"):
                    wallpaper_type = pdata.get("type").lower()
              except Exception:
                pass
        video_size = os.path.getsize(video_file) if video_file else 0
        with TASKS_LOCK:
          ACTIVE_TASKS[item_id] = {
              "status": "finished",
              "item_id": item_id,
              "message": "已存在于 NAS 硬盘，无需重复下载",
              "real_title": real_title,
              "wallpaper_type": wallpaper_type,
              "target_dir": md,
              "final_size": sz,
              "final_size_str": format_size(sz),
              "files": copied_files,
              "has_video": video_file is not None,
              "video_file": video_file,
              "video_size": video_size,
              "preview_file": preview_file,
              "percent": 100,
              "downloaded_bytes": sz,
              "total_bytes": sz,
              "total_str": format_size(sz),
              "downloaded_str": format_size(sz),
              "speed_str": "0 KB/s",
          }
        return

  # 2. Check if content_src in SteamCMD cache already has valid files (> 1MB)
  content_src = (
      f"{STEAMCMD_DATA_DIR}/Steam/steamapps/workshop/content/431960/{item_id}"
  )
  need_download = True
  if os.path.exists(content_src):
    src_size = get_dir_size(content_src)
    if src_size > 1024 * 1024:
      need_download = False
    else:
      try:
        shutil.rmtree(content_src)
      except Exception:
        pass

  with TASKS_LOCK:
    ACTIVE_TASKS[item_id]["status"] = "downloading"
    ACTIVE_TASKS[item_id]["percent"] = 0

  if need_download:
    stop_monitor = threading.Event()

    def _monitor():
      downloads_dir = (
          f"{STEAMCMD_DATA_DIR}/Steam/steamapps/workshop/downloads/431960"
      )
      content_item_dir = f"{STEAMCMD_DATA_DIR}/Steam/steamapps/workshop/content/431960/{item_id}"
      last_b = 0
      last_t = time.time()
      tracked_total = total_bytes

      while not stop_monitor.is_set():
        time.sleep(0.7)
        now = time.time()
        dt = max(0.1, now - last_t)

        d_size = get_dir_size(downloads_dir)
        c_size = get_dir_size(content_item_dir)
        cur_b = max(d_size, c_size)

        if tracked_total <= 0:
          log_p = f"{STEAMCMD_DATA_DIR}/Steam/logs/content_log.txt"
          if os.path.exists(log_p):
            try:
              with open(log_p, "r", encoding="utf-8", errors="ignore") as lf:
                lines = lf.readlines()[-25:]
                for l in reversed(lines):
                  m = re.search(r"update started : download 0/(\d+)", l)
                  if m:
                    tracked_total = int(m.group(1))
                    break
            except Exception:
              pass

        speed = max(0, (cur_b - last_b) / dt)
        last_b = cur_b
        last_t = now

        pct = 0
        if tracked_total > 0:
          pct = min(99, int((cur_b / tracked_total) * 100))

        with TASKS_LOCK:
          ACTIVE_TASKS[item_id] = {
              "status": "downloading",
              "item_id": item_id,
              "total_bytes": tracked_total,
              "downloaded_bytes": cur_b,
              "percent": pct,
              "speed_str": f"{format_size(speed)}/s",
              "downloaded_str": format_size(cur_b),
              "total_str": (
                  format_size(tracked_total) if tracked_total else "计算中..."
              ),
          }

    monitor_thread = threading.Thread(target=_monitor, daemon=True)
    monitor_thread.start()

    args = [
        "+login",
        login_user,
        "+workshop_download_item",
        "431960",
        item_id,
        "+quit",
    ]
    code_ret, output = run_steamcmd(args, timeout=3600)

    stop_monitor.set()
    monitor_thread.join(timeout=1.5)

  if not os.path.exists(content_src) or get_dir_size(content_src) < 1024:
    is_auth_error = False
    raw_out = output if output else ""
    if any(k in raw_out for k in ("Access Denied", "FAILED", "Logon Denied", "NeedTwoFactor")):
      is_auth_error = True
    else:
      conn_log_path = f"{STEAMCMD_DATA_DIR}/Steam/logs/connection_log.txt"
      if os.path.exists(conn_log_path):
        try:
          with open(conn_log_path, "r", encoding="utf-8", errors="ignore") as clf:
            recent_lines = "".join(clf.readlines()[-30:])
            if "Access Denied" in recent_lines or "LogonFailureReceived" in recent_lines:
              is_auth_error = True
        except Exception:
          pass

    if is_auth_error:
      cfg = load_config()
      cfg["logged_in"] = False
      save_config(cfg)
      with TASKS_LOCK:
        ACTIVE_TASKS[item_id] = {
            "status": "not_logged_in",
            "item_id": item_id,
            "message": (
                "Steam 账号登录凭据已过期或被官方注销（Access Denied）！"
                "请使用 /steam_login <账号> <密码> [验证码] 重新登录绑定拥有小红车的账号。"
            ),
        }
      return

    with TASKS_LOCK:
      ACTIVE_TASKS[item_id] = {
          "status": "error",
          "item_id": item_id,
          "message": "SteamCMD 下载失败或未找到目标文件",
      }
    return

  # Read project.json if available
  real_title = title
  wallpaper_type = "scene"
  project_json_path = os.path.join(content_src, "project.json")
  if os.path.exists(project_json_path):
    try:
      with open(project_json_path, "r", encoding="utf-8", errors="ignore") as pf:
        pdata = json.load(pf)
        if pdata.get("title"):
          real_title = pdata.get("title").strip()
        if pdata.get("type"):
          wallpaper_type = pdata.get("type").lower()
    except Exception:
      pass

  clean_title = re.sub(r'[\/:*?"<>|]', "_", real_title).strip()
  target_folder_name = f"{item_id} - {clean_title[:50]}"
  target_dir = os.path.join(DOWNLOADS_BASE, target_folder_name)
  os.makedirs(target_dir, exist_ok=True)

  copied_files = []
  video_file = None
  preview_file = None

  for root, dirs, files in os.walk(content_src):
    for f in files:
      src_file = os.path.join(root, f)
      rel_path = os.path.relpath(src_file, content_src)
      dst_file = os.path.join(target_dir, rel_path)
      os.makedirs(os.path.dirname(dst_file), exist_ok=True)
      shutil.copy2(src_file, dst_file)
      copied_files.append(rel_path)

      lower_f = f.lower()
      if lower_f.endswith((".mp4", ".webm", ".mkv")):
        video_file = dst_file
      elif lower_f.startswith("preview") and lower_f.endswith(
          (".gif", ".jpg", ".jpeg", ".png")
      ):
        preview_file = dst_file

  os.system(f"chmod -R 777 '{target_dir}'")
  final_size = get_dir_size(target_dir)
  video_size = os.path.getsize(video_file) if video_file else 0

  with TASKS_LOCK:
    ACTIVE_TASKS[item_id] = {
        "status": "finished",
        "item_id": item_id,
        "message": "成功下载并保存至 NAS 硬盘",
        "real_title": real_title,
        "wallpaper_type": wallpaper_type,
        "target_dir": target_dir,
        "final_size": final_size,
        "final_size_str": format_size(final_size),
        "files": copied_files,
        "has_video": video_file is not None,
        "video_file": video_file,
        "video_size": video_size,
        "preview_file": preview_file,
        "percent": 100,
        "downloaded_bytes": final_size,
        "total_bytes": final_size,
        "total_str": format_size(final_size),
        "downloaded_str": format_size(final_size),
        "speed_str": "0 KB/s",
    }


class UnixHTTPConnection(http.client.HTTPConnection):
  def __init__(self, socket_path):
    super().__init__('localhost')
    self.socket_path = socket_path

  def connect(self):
    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    self.sock.connect(self.socket_path)


def get_fnos_qbt_credentials(sock_path: str, proc_root: str = "/proc") -> dict:
  """Read fnOS's current engine credentials without persisting or logging them."""
  try:
    processes = Path(proc_root).iterdir()
    for process in processes:
      if not process.name.isdecimal():
        continue
      try:
        arguments = (process / "cmdline").read_bytes().decode().split("\0")
      except (OSError, UnicodeError):
        continue
      if not arguments or Path(arguments[0]).name != "trim-qbittorrent-nox":
        continue
      options = {}
      for argument in arguments[1:]:
        key, separator, value = argument.partition("=")
        if separator and key in {"--webui-sock-path", "--webui-password"}:
          options[key] = value
      if (
          options.get("--webui-sock-path") == sock_path
          and options.get("--webui-password")
      ):
        return {"username": "admin", "password": options["--webui-password"]}
  except OSError:
    pass
  return {}


def get_qbt_cookie(sock_path: str = "/home/17354371800/qbt.sock") -> str:
  configuration = get_fnos_qbt_credentials(sock_path) or load_config().get("qbt", {})
  username = os.environ.get("QBT_USERNAME") or configuration.get("username")
  password = os.environ.get("QBT_PASSWORD") or configuration.get("password")
  if not username or not password:
    return ""
  conn = None
  try:
    conn = UnixHTTPConnection(sock_path)
    conn.request(
        "POST", "/api/v2/auth/login",
        urllib.parse.urlencode({"username": username, "password": password}),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    res = conn.getresponse()
    cookie = res.getheader("Set-Cookie")
    body = res.read().decode("utf-8", errors="replace").strip()
    return cookie.split(";")[0] if res.status == 200 and body == "Ok." and cookie else ""
  except Exception:
    return ""
  finally:
    if conn:
      conn.close()


def format_bytes_size(b: float) -> str:
  for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
    if b < 1024: return f"{b:.1f} {unit}"
    b /= 1024
  return f"{b:.1f} PB"


def add_torrent_to_qbt(magnet: str, save_path: str = "/vol1/1000/总和/娱乐", category: str = "动漫") -> dict:
  """Sends magnet or torrent link to fnOS built-in qBittorrent via unix socket."""
  sock_path = "/home/17354371800/qbt.sock"
  if not os.path.exists(sock_path):
    return {"status": "error", "message": f"qBittorrent 套接字未找到: {sock_path}"}

  sid = get_qbt_cookie(sock_path)
  if not sid:
    return {"status": "error", "message": "qBittorrent 登录授权失败"}

  try:
    conn2 = UnixHTTPConnection(sock_path)
    data = urllib.parse.urlencode({
        "urls": magnet,
        "savepath": save_path,
        "category": category,
        "paused": "false"
    })
    headers = {"Cookie": sid, "Content-Type": "application/x-www-form-urlencoded"}
    conn2.request("POST", "/api/v2/torrents/add", data, headers)
    res2 = conn2.getresponse()
    resp_text = res2.read().decode("utf-8", errors="ignore").strip()
    if res2.status == 200:
      if resp_text == "Ok.":
        return {"status": "ok", "message": "磁力任务已成功推送到 NAS 下载队列！", "save_path": save_path}
      elif resp_text == "Fails.":
        # 任务已存在于 qbt 中，检测其实际下载进度与本地物理文件状态
        m = re.search(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet, re.IGNORECASE)
        info_hash = m.group(1).lower() if m else ""
        if info_hash:
          try:
            conn3 = UnixHTTPConnection(sock_path)
            conn3.request("GET", f"/api/v2/torrents/info?hashes={urllib.parse.quote(info_hash)}", headers={"Cookie": sid})
            existing_body = conn3.getresponse().read().decode("utf-8", errors="ignore")
            existing_list = json.loads(existing_body) if existing_body else []
            if not existing_list:
              conn3_all = UnixHTTPConnection(sock_path)
              conn3_all.request("GET", "/api/v2/torrents/info", headers={"Cookie": sid})
              all_t = json.loads(conn3_all.getresponse().read().decode("utf-8", errors="ignore"))
              for it in all_t:
                if it.get("hash", "").lower().startswith(info_hash):
                  existing_list = [it]
                  break

            if existing_list:
              t = existing_list[0]
              content_path = t.get("content_path", "")
              progress = float(t.get("progress", 0.0))
              state = t.get("state", "")
              file_exists = os.path.exists(content_path) if content_path else False

              # 1. 真实已完成且磁盘文件完好存在
              if file_exists and progress >= 1.0:
                return {
                    "status": "ok",
                    "already_exists": True,
                    "already_completed": True,
                    "name": t.get("name"),
                    "message": "该资源此前已下载完成并存在于 NAS 硬盘中！无需重复下载。",
                    "save_path": t.get("save_path", save_path),
                    "content_path": content_path,
                    "total_size": t.get("total_size", 0),
                    "total_size_str": format_bytes_size(t.get("total_size", 0)),
                }

              # 2. 任务正在下载运行中
              if 0.0 < progress < 1.0 and state not in ("error", "missingFiles"):
                return {
                    "status": "ok",
                    "already_exists": True,
                    "already_completed": False,
                    "is_downloading": True,
                    "name": t.get("name"),
                    "message": f"该任务正在 NAS 队列中下载运行（当前进度 {round(progress*100, 1)}%）",
                    "save_path": t.get("save_path", save_path),
                    "content_path": content_path,
                    "progress": progress,
                }

              # 3. 历史残留或文件丢失 (progress >= 1.0 但文件已被移动/删除，或 state 异常)
              # 用户请求下载说明需要重新拉取，清理旧残留并重新添加
              conn_del = UnixHTTPConnection(sock_path)
              del_data = urllib.parse.urlencode({"hashes": info_hash, "deleteFiles": "false"})
              conn_del.request("POST", "/api/v2/torrents/delete", del_data, headers={"Cookie": sid, "Content-Type": "application/x-www-form-urlencoded"})
              conn_del.getresponse().read()
              time.sleep(0.5)

              conn_add = UnixHTTPConnection(sock_path)
              conn_add.request("POST", "/api/v2/torrents/add", data, headers)
              res_add = conn_add.getresponse()
              add_text = res_add.read().decode("utf-8", errors="ignore").strip()
              if res_add.status == 200 and add_text == "Ok.":
                return {
                    "status": "ok",
                    "readded": True,
                    "name": t.get("name"),
                    "message": "已自动清理历史残留记录，重新开始下载！",
                    "save_path": save_path,
                }
          except Exception as check_err:
            pass

        return {"status": "ok", "already_exists": True, "message": "该资源已在 NAS 下载列表中（无需重复添加）", "save_path": save_path}
      else:
        return {"status": "error", "message": f"添加失败: HTTP {res2.status} {resp_text}"}
    else:
      return {"status": "error", "message": f"添加失败: HTTP {res2.status} {resp_text}"}
  except Exception as e:
    return {"status": "error", "message": f"连接 qBittorrent 异常: {e}"}


def get_qbt_torrent_info(info_hash: str) -> dict:
  """Get torrent info from qBittorrent via unix socket."""
  sock_path = "/home/17354371800/qbt.sock"
  if not os.path.exists(sock_path):
    return {"status": "error", "message": "qbt.sock not found"}

  sid = get_qbt_cookie(sock_path)
  if not sid:
    return {"status": "error", "message": "auth failed"}

  try:
    conn = UnixHTTPConnection(sock_path)
    clean_hash = info_hash.strip().lower()
    conn.request("GET", f"/api/v2/torrents/info?hashes={urllib.parse.quote(clean_hash)}", headers={"Cookie": sid})
    res = conn.getresponse()
    body = res.read().decode("utf-8", errors="ignore")
    torrents = json.loads(body) if body else []
    if not torrents:
      # Try matching partial hash prefix against all torrents
      conn2 = UnixHTTPConnection(sock_path)
      conn2.request("GET", "/api/v2/torrents/info", headers={"Cookie": sid})
      all_t = json.loads(conn2.getresponse().read().decode("utf-8", errors="ignore"))
      for t in all_t:
        if t.get("hash", "").lower().startswith(clean_hash):
          torrents = [t]
          break

    if not torrents:
      return {"status": "not_found", "message": "任务未在队列中"}

    t = torrents[0]
    progress = float(t.get("progress", 0.0))
    pct = round(progress * 100, 1)
    total_size = t.get("total_size", 0)
    downloaded = t.get("downloaded", 0)
    dlspeed = t.get("dlspeed", 0)
    upspeed = t.get("upspeed", 0)
    state = t.get("state", "")
    eta = t.get("eta", 8640000)

    def _sz(b):
      for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024: return f"{b:.1f} {unit}"
        b /= 1024
      return f"{b:.1f} PB"

    def _eta_str(sec):
      if sec >= 8640000 or sec < 0: return "未知"
      if sec < 60: return f"{sec}秒"
      if sec < 3600: return f"{sec//60}分{sec%60}秒"
      return f"{sec//3600}小时{(sec%3600)//60}分"

    state_map = {
      "metaDL": "拉取元数据",
      "allocating": "预分配空间",
      "downloading": "高速下载中",
      "forcedDL": "强制下载中",
      "stalledDL": "寻找做种中",
      "queuedDL": "排队下载中",
      "checkingDL": "校验文件中",
      "pausedDL": "已暂停下载",
      "uploading": "已完成(做种中)",
      "pausedUP": "已完成(暂停)",
      "stalledUP": "已完成(做种待命)",
      "checkingUP": "做种校验中",
      "queuedUP": "排队做种中",
      "error": "任务异常",
      "missingFiles": "文件丢失",
    }

    is_complete = progress >= 1.0 or state in ("uploading", "pausedUP", "stalledUP")

    return {
      "status": "ok",
      "found": True,
      "hash": t.get("hash"),
      "name": t.get("name"),
      "progress": progress,
      "percent": pct,
      "total_size": total_size,
      "total_size_str": _sz(total_size),
      "downloaded": downloaded,
      "downloaded_str": _sz(downloaded),
      "dlspeed": dlspeed,
      "dlspeed_str": f"{_sz(dlspeed)}/s",
      "upspeed": upspeed,
      "upspeed_str": f"{_sz(upspeed)}/s",
      "eta": eta,
      "eta_str": _eta_str(eta),
      "state": state,
      "state_desc": state_map.get(state, state),
      "seeds": t.get("num_seeds", 0),
      "leechs": t.get("num_leechs", 0),
      "is_complete": is_complete,
      "save_path": t.get("save_path", ""),
      "content_path": t.get("content_path", "")
    }
  except Exception as e:
    return {"status": "error", "message": str(e)}


def get_qbt_torrent_list() -> dict:
  """Get list of active torrents from qBittorrent via unix socket."""
  sock_path = "/home/17354371800/qbt.sock"
  if not os.path.exists(sock_path):
    return {"status": "error", "message": "qbt.sock not found", "items": []}

  sid = get_qbt_cookie(sock_path)
  if not sid:
    return {"status": "error", "message": "auth failed", "items": []}

  try:
    conn = UnixHTTPConnection(sock_path)
    conn.request("GET", "/api/v2/torrents/info", headers={"Cookie": sid})
    res = conn.getresponse()
    body = res.read().decode("utf-8", errors="ignore")
    torrents = json.loads(body) if body else []

    def _sz(b):
      for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if b < 1024: return f"{b:.1f} {unit}"
        b /= 1024
      return f"{b:.1f} PB"

    state_map = {
      "metaDL": "拉取元数据",
      "allocating": "预分配空间",
      "downloading": "高速下载中",
      "stalledDL": "寻找做种中",
      "queuedDL": "排队中",
      "pausedDL": "已暂停",
      "uploading": "已完成(做种)",
      "pausedUP": "已完成",
      "stalledUP": "已完成",
      "error": "异常",
    }

    items = []
    for t in torrents:
      progress = float(t.get("progress", 0.0))
      items.append({
        "hash": t.get("hash"),
        "name": t.get("name"),
        "percent": round(progress * 100, 1),
        "total_size_str": _sz(t.get("total_size", 0)),
        "downloaded_str": _sz(t.get("downloaded", 0)),
        "dlspeed_str": f"{_sz(t.get('dlspeed', 0))}/s",
        "state": t.get("state", ""),
        "state_desc": state_map.get(t.get("state", ""), t.get("state", "")),
        "is_complete": progress >= 1.0 or t.get("state", "") in ("uploading", "pausedUP", "stalledUP")
      })
    return {"status": "ok", "items": items}
  except Exception as e:
    return {"status": "error", "message": str(e), "items": []}


def delete_qbt_torrent(info_hash: str, delete_files: bool = True) -> dict:
  """Deletes a torrent from fnOS built-in qBittorrent via unix socket."""
  sock_path = "/home/17354371800/qbt.sock"
  if not os.path.exists(sock_path):
    return {"status": "error", "message": f"qBittorrent 套接字未找到: {sock_path}"}

  sid = get_qbt_cookie(sock_path)
  if not sid:
    return {"status": "error", "message": "qBittorrent 登录授权失败"}

  try:
    clean_hash = info_hash.strip().lower()
    full_hash = clean_hash
    if len(clean_hash) < 40:
      conn2 = UnixHTTPConnection(sock_path)
      conn2.request("GET", "/api/v2/torrents/info", headers={"Cookie": sid})
      all_t = json.loads(conn2.getresponse().read().decode("utf-8", errors="ignore"))
      conn2.close()
      for t in all_t:
        if t.get("hash", "").lower().startswith(clean_hash):
          full_hash = t.get("hash")
          break

    conn = UnixHTTPConnection(sock_path)
    data = urllib.parse.urlencode({
        "hashes": full_hash,
        "deleteFiles": "true" if delete_files else "false",
    })
    headers = {"Cookie": sid, "Content-Type": "application/x-www-form-urlencoded"}
    conn.request("POST", "/api/v2/torrents/delete", data, headers)
    res = conn.getresponse()
    conn.close()
    if res.status == 200:
      return {"status": "ok", "message": "任务已从 qBittorrent 成功删除"}
    return {"status": "error", "message": f"qBt 返回状态: {res.status}"}
  except Exception as e:
    return {"status": "error", "message": str(e)}


def worker_loop():
  """Background sequential worker for downloading workshop items."""
  while True:
    task = DOWNLOAD_QUEUE.get()
    item_id = task["item_id"]
    title = task.get("title", f"wallpaper_{item_id}")
    total_bytes = task.get("total_bytes", 0)
    with TASKS_LOCK:
      if ACTIVE_TASKS.get(item_id, {}).get("status") == "cancelled":
        DOWNLOAD_QUEUE.task_done()
        continue
    try:
      process_item_download(item_id, title, total_bytes)
    except Exception as e:
      with TASKS_LOCK:
        ACTIVE_TASKS[item_id] = {
            "status": "error",
            "item_id": item_id,
            "message": f"下载处理异常: {e}",
        }
    finally:
      DOWNLOAD_QUEUE.task_done()


class SteamHandler(http.server.BaseHTTPRequestHandler):

  def _send_json(self, data: dict, status: int = 200):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def do_GET(self):
    url = urlparse(self.path)
    if url.path == "/status":
      cfg = load_config()
      return self._send_json({
          "status": "ok",
          "logged_in": cfg.get("logged_in", False),
          "current_user": cfg.get("current_user"),
      })

    elif url.path == "/progress":
      query = parse_qs(url.query)
      item_id = query.get("item_id", [""])[0]
      with TASKS_LOCK:
        task = ACTIVE_TASKS.get(item_id)
        if task:
          return self._send_json(task)
      return self._send_json({"status": "idle", "percent": 0})

    elif url.path in ("/qbt/status", "/api/qbt/status"):
      sock_path = "/home/17354371800/qbt.sock"
      return self._send_json({"status": "ok", "qbt_online": os.path.exists(sock_path)})

    elif url.path in ("/qbt/progress", "/api/qbt/progress", "/qbt/info"):
      query = parse_qs(url.query)
      info_hash = query.get("hash", [""])[0]
      return self._send_json(get_qbt_torrent_info(info_hash))

    elif url.path in ("/qbt/list", "/api/qbt/list"):
      return self._send_json(get_qbt_torrent_list())

    elif url.path in ("/cancel", "/api/cancel"):
      query = parse_qs(url.query)
      item_id = query.get("item_id", [""])[0]
      with TASKS_LOCK:
        if item_id in ACTIVE_TASKS:
          ACTIVE_TASKS[item_id]["status"] = "cancelled"
          ACTIVE_TASKS[item_id]["message"] = "用户已取消下载"
      return self._send_json({"status": "ok", "message": "任务已标记取消"})

    elif url.path in ("/qbt/delete", "/api/qbt/delete"):
      query = parse_qs(url.query)
      info_hash = query.get("hash", [""])[0] or query.get("info_hash", [""])[0]
      delete_files = query.get("delete_files", ["true"])[0].lower() in ("true", "1")
      if not info_hash:
        return self._send_json({"status": "error", "message": "缺少 hash 参数"}, 400)
      return self._send_json(delete_qbt_torrent(info_hash, delete_files))

    self._send_json({"error": "Not Found"}, 404)

  def do_POST(self):
    url = urlparse(self.path)
    content_len = int(self.headers.get("Content-Length", 0))
    post_data = self.rfile.read(content_len).decode("utf-8")
    try:
      params = json.loads(post_data) if post_data else {}
    except Exception:
      params = {}

    if url.path == "/login":
      username = params.get("username", "").strip()
      password = params.get("password", "").strip()
      code = params.get("code", "").strip()

      if not username or not password:
        return self._send_json({"status": "error", "message": "缺少账号或密码"}, 400)

      args = ["+login", username, password]
      if code:
        args.append(code)
      args.append("+quit")

      code_ret, output = run_steamcmd(args, timeout=60)
      clean_output = re.sub(r"\x1b\[[0-9;]*[mGKH]", "", output)

      is_success = (
          "Logged in OK" in clean_output
          or "Waiting for user info..." in clean_output
          or "post-logon" in clean_output
          or ("Logging in user" in clean_output and "OK" in clean_output)
      ) and not (
          "FAILED" in clean_output
          or "Invalid Password" in clean_output
          or "NeedTwoFactor" in clean_output
          or "Steam Guard code:" in clean_output
      )

      if is_success:
        cfg = load_config()
        cfg["current_user"] = username
        cfg["logged_in"] = True
        save_config(cfg)
        return self._send_json({
            "status": "ok",
            "message": f"Steam 账号 [{username}] 登录并授权成功！凭据已安全持久化缓存。",
        })
      elif "AccountLoginDeniedNeedTwoFactor" in clean_output or (
          "Steam Guard code:" in clean_output and not code
      ):
        return self._send_json({
            "status": "need_guard",
            "message": (
                "需要 Steam 手机手机令牌 / 邮箱验证码！请带上验证码重新执行，例如："
                " /steam_login <账号> <密码> <验证码>"
            ),
        })
      elif "Invalid Password" in clean_output:
        return self._send_json({"status": "error", "message": "Steam 账号或密码错误"})
      elif "RateLimitExceeded" in clean_output:
        return self._send_json({
            "status": "error",
            "message": "Steam 登录请求过于频繁，请稍后重试",
        })
      else:
        clean_lines = [
            line.strip()
            for line in clean_output.splitlines()
            if line.strip() and not line.strip().startswith("[")
        ]
        last_lines = "\n".join(clean_lines[-4:])
        return self._send_json({
            "status": "error",
            "message": f"SteamCMD 登录反馈:\n{last_lines}",
        })

    elif url.path == "/logout":
      cfg = load_config()
      cfg["current_user"] = None
      cfg["logged_in"] = False
      save_config(cfg)
      return self._send_json({"status": "ok", "message": "已登出并清除 Steam 绑定"})

    elif url.path == "/download":
      item_id = str(params.get("item_id", "")).strip()
      title = params.get("title", f"wallpaper_{item_id}").strip()
      total_bytes = int(params.get("total_bytes", 0))

      if not item_id or not item_id.isdigit():
        return self._send_json(
            {"status": "error", "message": "无效的创意工坊 ID"}, 400
        )

      cfg = load_config()
      login_user = cfg.get("current_user")
      if not login_user or not cfg.get("logged_in"):
        return self._send_json({
            "status": "not_logged_in",
            "message": (
                "尚未登录 Steam 账号！由于小红车为付费软件，请先使用 /steam_login"
                " 登录一次拥有小红车的 Steam 账号。"
            ),
        })

      with TASKS_LOCK:
        if item_id in ACTIVE_TASKS:
          curr = ACTIVE_TASKS[item_id]
          if curr.get("status") == "finished":
            return self._send_json(curr)
          elif curr.get("status") in ("downloading", "queued"):
            return self._send_json({
                "status": "queued",
                "message": "任务已在队列中",
                "item_id": item_id,
                "queue_pos": curr.get("queue_pos", 1),
            })

        pos = DOWNLOAD_QUEUE.qsize() + 1
        ACTIVE_TASKS[item_id] = {
            "status": "queued",
            "item_id": item_id,
            "title": title,
            "queue_pos": pos,
            "total_bytes": total_bytes,
            "downloaded_bytes": 0,
            "percent": 0,
            "speed_str": "0 KB/s",
            "downloaded_str": "0 B",
            "total_str": format_size(total_bytes) if total_bytes else "未知大小",
        }
        DOWNLOAD_QUEUE.put({
            "item_id": item_id,
            "title": title,
            "total_bytes": total_bytes,
        })

      return self._send_json({
          "status": "queued",
          "message": "已加入下载队列",
          "item_id": item_id,
          "queue_pos": pos,
      })

    elif url.path in ("/qbt/add", "/api/qbt/add"):
      magnet = params.get("magnet") or params.get("url") or ""
      save_path = params.get("save_path") or "/vol1/1000/总和/娱乐"
      category = params.get("category") or "动漫"
      if not magnet:
        return self._send_json({"status": "error", "message": "缺少 magnet 磁力链接"}, 400)
      res = add_torrent_to_qbt(magnet, save_path, category)
      return self._send_json(res)
    elif url.path in ("/cancel", "/api/cancel"):
      item_id = str(params.get("item_id") or "").strip()
      with TASKS_LOCK:
        if item_id in ACTIVE_TASKS:
          ACTIVE_TASKS[item_id]["status"] = "cancelled"
          ACTIVE_TASKS[item_id]["message"] = "用户已取消下载"
      return self._send_json({"status": "ok", "message": "任务已标记取消"})

    elif url.path in ("/qbt/delete", "/api/qbt/delete"):
      info_hash = str(params.get("hash") or params.get("info_hash") or "").strip()
      delete_files = bool(params.get("delete_files", True))
      if not info_hash:
        return self._send_json({"status": "error", "message": "缺少 hash 参数"}, 400)
      return self._send_json(delete_qbt_torrent(info_hash, delete_files))

    self._send_json({"error": "Not Found"}, 404)


if __name__ == "__main__":
  os.makedirs(STEAMCMD_DATA_DIR, exist_ok=True)
  os.makedirs(DOWNLOADS_BASE, exist_ok=True)
  worker_thread = threading.Thread(target=worker_loop, daemon=True)
  worker_thread.start()
  server = http.server.ThreadingHTTPServer((HOST, PORT), SteamHandler)
  print(f"Steam Workshop Microservice running on {HOST}:{PORT}")
  server.serve_forever()
