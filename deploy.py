#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 dashboard 推送到 GitHub Pages 仓库（通过 GitHub Contents API，无需本地 git，可绕过沙箱代理 TLS 问题）。
- 仅推送 index.html 与 all_data.json（dashboard 根目录即仓库根）。
- 凭据优先级：环境变量 GH_USER / GH_REPO / GH_PAT > deploy_config.json。
- PAT 需 classic token，勾选 repo 权限。
用法：
  set GH_USER=yourname&& set GH_REPO=petroleum-loss-dashboard&& set GH_PAT=ghp_xxx&& python deploy.py
  或在 deploy_config.json 填入 {"user":..,"repo":..,"pat":..}（勿提交该文件）。
"""
import os, sys, json, base64, datetime, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = {}
cfgp = os.path.join(HERE, "deploy_config.json")
if os.path.exists(cfgp):
    try:
        cfg = json.load(open(cfgp, encoding="utf-8"))
    except Exception:
        cfg = {}

USER = os.environ.get("GH_USER") or cfg.get("user")
REPO = os.environ.get("GH_REPO") or cfg.get("repo")
PAT = os.environ.get("GH_PAT") or cfg.get("pat")
BRANCH = os.environ.get("GH_BRANCH") or cfg.get("branch") or "main"

if not (USER and REPO and PAT):
    print("缺少配置：请设置环境变量 GH_USER / GH_REPO / GH_PAT，或在 deploy_config.json 填写。")
    sys.exit(1)

API = "https://api.github.com"
HDR = {
    "Authorization": "Bearer " + PAT,
    "User-Agent": "dashboard-deploy",
    "Accept": "application/vnd.github+json",
    "Content-Type": "application/json",
}

def api_call(method, path, data=None):
    req = urllib.request.Request(API + path, method=method, headers=HDR)
    if data is not None:
        req.data = json.dumps(data).encode("utf-8")
    try:
        r = urllib.request.urlopen(req, timeout=180)
        return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            body = e.read().decode("utf-8", "replace")
        return e.code, body
    except Exception as e:
        return -1, str(e)

def push_file(local_rel, msg):
    local = os.path.join(HERE, local_rel)
    if not os.path.exists(local):
        print("跳过（本地缺失）: " + local_rel)
        return True
    path = "/repos/%s/%s/contents/%s" % (USER, REPO, local_rel)
    st, cur = api_call("GET", path)
    sha = cur.get("sha") if st == 200 else None
    with open(local, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    payload = {"message": msg, "content": b64, "branch": BRANCH}
    if sha:
        payload["sha"] = sha
    st2, res = api_call("PUT", path, payload)
    if st2 in (200, 201):
        size = res.get("content", {}).get("size") if isinstance(res, dict) else None
        print("OK  %s -> %s bytes" % (local_rel, size))
        return True
    info = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
    print("FAIL %s -> HTTP %s: %s" % (local_rel, st2, info[:200]))
    return False

msg = "sync " + datetime.date.today().isoformat()
ok = True
for f in ["index.html", "all_data.json"]:
    if not push_file(f, msg):
        ok = False

if ok:
    print("OK -> https://%s.github.io/%s/" % (USER, REPO))
    sys.exit(0)
else:
    print("部分文件推送失败。")
    sys.exit(1)
