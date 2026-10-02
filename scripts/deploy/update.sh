#!/usr/bin/env bash
#
# 知途后端安全更新脚本。
#
# ## 它解决什么
#
# 生产更新最容易出错的两件事:**部署了没验收的代码**,以及**迁移失败后把服务留在
# 半死状态却报告成功**。这个脚本把两件事都堵死:
#
#   1. 必须显式给出**完整 commit SHA**(`--ref`),不接受分支名/短 SHA。
#      它只 `git checkout --detach <sha>`,**从不 pull 一个移动分支** ——
#      部署的东西永远可追溯。
#   2. 任一步失败就打印失败步骤 + `systemctl status` + `journalctl`,**不自动
#      downgrade**,并明确要求用备份或旧 commit 人工回滚。绝不把失败伪装成成功。
#
# ## 它**不**做什么
#
#   - 不备份数据库。**备份由操作者先做**,再用 `--backup` 把文件路径传进来
#     (脚本只校验:存在、非空、24 小时内)。
#   - 不碰 Vercel、不写前端密钥、不跑 Docker。
#   - 不输出 `DATABASE_URL` / `LLM_API_KEY` / `APP_SECRET_KEY` 等任何密钥值 ——
#     只检查 `.env` 里有没有这些键。
#
# ## 用法
#
#   sudo scripts/deploy/update.sh --ref <40位commit SHA> --backup /srv/backups/xxx.dump
#   sudo scripts/deploy/update.sh --dry-run --ref <SHA> --backup <path>
#
# 可配置项见 --help。默认值按仓库自带的部署手册(`docs/08-DEPLOYMENT.md`):
# repo-dir=/srv/zhitu、service=zhitu-api、user=zhitu、api-url=http://127.0.0.1:8000。

set -euo pipefail

REPO_DIR="/srv/zhitu"
SERVICE="zhitu-api"
RUN_USER="zhitu"
API_URL="http://127.0.0.1:8000"
REF=""
BACKUP=""
DRY_RUN=0

usage() {
  cat <<'EOF'
用法:
  update.sh --ref <完整40位commit SHA> --backup <数据库备份文件路径> [选项]

必填:
  --ref <SHA>        要部署的**完整** commit SHA(40 位十六进制)。不接受分支名/短 SHA。
  --backup <路径>    部署前已做好的数据库备份文件;必须存在、非空、24 小时内。

选项:
  --repo-dir <路径>  仓库目录(默认 /srv/zhitu)
  --service <名字>   systemd 服务名(默认 zhitu-api)
  --user <名字>      运行后端进程/命令的系统用户(默认 zhitu)
  --api-url <URL>    就绪探针地址(默认 http://127.0.0.1:8000)
  --dry-run          只做前置检查并打印将执行的步骤,不停服务、不迁移、不写任何东西
  -h, --help         显示本帮助

示例:
  sudo scripts/deploy/update.sh --ref 2bca4e0e6a... --backup /srv/backups/zhitu-20261002.dump
  sudo scripts/deploy/update.sh --dry-run --ref <SHA> --backup <path>
EOF
}

log() { printf '[update] %s\n' "$*"; }
die() { printf '[update] ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --repo-dir) [ $# -ge 2 ] || die "--repo-dir 缺少值"; REPO_DIR="$2"; shift 2 ;;
    --service)  [ $# -ge 2 ] || die "--service 缺少值";  SERVICE="$2";  shift 2 ;;
    --user)     [ $# -ge 2 ] || die "--user 缺少值";     RUN_USER="$2"; shift 2 ;;
    --api-url)  [ $# -ge 2 ] || die "--api-url 缺少值";  API_URL="$2";  shift 2 ;;
    --ref)      [ $# -ge 2 ] || die "--ref 缺少值";      REF="$2";      shift 2 ;;
    --backup)   [ $# -ge 2 ] || die "--backup 缺少值";   BACKUP="$2";   shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) die "未知参数:$1(用 --help 看用法)" ;;
  esac
done

# ---------------------------------------------------------------------------------
# 参数校验(在任何动作之前)
# ---------------------------------------------------------------------------------
[ -n "$REF" ] || die "必须显式指定 --ref <完整 40 位 commit SHA>;不接受分支名或短 SHA。"
case "$REF" in
  *[!0-9a-fA-F]*) die "--ref 必须是完整的 commit SHA(40 位十六进制)" ;;
esac
[ "${#REF}" -eq 40 ] || die "--ref 必须是 40 位完整 SHA(收到 ${#REF} 位)。短 SHA 不接受。"
[ -n "$BACKUP" ] || die "必须显式指定 --backup <数据库备份文件路径>;备份请在本脚本之前完成。"

#: 以服务用户执行命令。脚本本身需要 root(systemctl)。非 root 时(例如 dry-run 的
#: 静态测试)直接执行,不做 sudo 提权。
as_user() {
  if [ "$(id -u)" -eq 0 ]; then
    if command -v runuser >/dev/null 2>&1; then runuser -u "$RUN_USER" -- "$@"
    else sudo -u "$RUN_USER" -- "$@"; fi
  else
    "$@"
  fi
}

git_c() { as_user git -C "$REPO_DIR" "$@"; }

# ---------------------------------------------------------------------------------
# 前置检查。**任一失败立即退出,且此时服务还没被停过。**
# ---------------------------------------------------------------------------------
check_repo() {
  [ -d "$REPO_DIR/.git" ] || die "$REPO_DIR 不是一个 git 仓库。"
  local dirty
  dirty="$(git_c status --porcelain 2>/dev/null || true)"
  [ -z "$dirty" ] || die "工作树不干净,先处理 $REPO_DIR(有未提交改动,拒绝继续)。"
  git_c cat-file -e "${REF}^{commit}" 2>/dev/null \
    || die "commit $REF 还没有 fetch 到本机(先 git fetch --all --prune 再试)。"
}

check_backup() {
  [ -f "$BACKUP" ] || die "备份文件不存在:$BACKUP"
  [ -s "$BACKUP" ] || die "备份文件是空的:$BACKUP"
  local now mtime age
  now="$(date +%s)"
  mtime="$(stat -c %Y "$BACKUP" 2>/dev/null || stat -f %m "$BACKUP")"
  age=$(( now - mtime ))
  [ "$age" -le 86400 ] || die "备份文件已超过 24 小时(${age}s);请重新备份后再更新。"
}

check_environment() {
  command -v systemctl >/dev/null 2>&1 || die "这台机器没有 systemd/systemctl。"
  systemctl cat "$SERVICE" >/dev/null 2>&1 || die "没有找到 systemd 服务 $SERVICE。"
  [ -x "$REPO_DIR/.venv/bin/python" ] || die "找不到虚拟环境:$REPO_DIR/.venv/bin/python"
  as_user "$REPO_DIR/.venv/bin/python" -c 'import sqlalchemy' >/dev/null 2>&1 \
    || die "虚拟环境里没有 sqlalchemy,先装 backend/requirements.txt。"
  [ -f "$REPO_DIR/.env" ] || die "找不到 $REPO_DIR/.env。"
  # 只检查键在不在,**不打印值**。
  grep -q '^DATABASE_URL=' "$REPO_DIR/.env" || die ".env 里没有 DATABASE_URL。"
  grep -q '^APP_SECRET_KEY=' "$REPO_DIR/.env" || die ".env 里没有 APP_SECRET_KEY。"
}

log "前置检查 …"
check_repo
check_backup

if [ "$DRY_RUN" -eq 1 ]; then
  OLD_COMMIT="$(git_c rev-parse HEAD)"
  VENV="$REPO_DIR/.venv"
  cat <<EOF
[update] dry-run:只打印将执行的步骤,不会停服务、不迁移、不写任何东西。
[update]   repo-dir : $REPO_DIR
[update]   service  : $SERVICE
[update]   user     : $RUN_USER
[update]   current  : $OLD_COMMIT
[update]   target   : $REF
[update]   backup   : $BACKUP
[update] 将依次执行:
  1. systemctl stop $SERVICE
  2. git -C $REPO_DIR fetch --all --prune
  3. git -C $REPO_DIR checkout --detach $REF
  4. $VENV/bin/pip install -r backend/requirements.txt
  5. [仅当已安装 openjiuwen] $VENV/bin/pip install -r backend/requirements-agent.txt
  6. (cd $REPO_DIR) $VENV/bin/python -m alembic -c backend/alembic.ini upgrade head
  7. alembic current 必须等于 alembic heads
  8. systemctl start $SERVICE
  9. curl $API_URL/ready
[update] 环境检查(venv/.env/systemd)在 dry-run 下跳过;真正执行时会先做。
EOF
  exit 0
fi

check_environment

# ---------------------------------------------------------------------------------
# 下面开始真正的更新。任何一步失败都会走 on_error。
# ---------------------------------------------------------------------------------
FAILED_STEP="(未开始)"
on_error() {
  local code=$?
  printf '\n[update] 失败:最后执行的步骤是「%s」(退出码 %s)\n' "$FAILED_STEP" "$code"
  printf '[update] === systemctl status %s ===\n' "$SERVICE"
  systemctl status "$SERVICE" --no-pager 2>&1 | tail -n 30 || true
  printf '[update] === journalctl -u %s -n 200 ===\n' "$SERVICE"
  journalctl -u "$SERVICE" -n 200 --no-pager 2>&1 || true
  printf '%s\n' \
    '[update] 没有自动回滚,也不会执行 alembic downgrade。' \
    '[update] 人工回滚:用本次 --backup 的备份恢复数据库,或把代码切回上一个已验证 commit 再重启。'
  exit "$code"
}
trap on_error ERR

VENV="$REPO_DIR/.venv"
AGENT_REQ="$REPO_DIR/backend/requirements-agent.txt"
OLD_COMMIT="$(git_c rev-parse HEAD)"
log "当前 commit:$OLD_COMMIT;目标 commit:$REF"

FAILED_STEP="停止服务"
log "停止 $SERVICE …"
systemctl stop "$SERVICE"

FAILED_STEP="fetch"
log "fetch 远端 …"
as_user git -C "$REPO_DIR" fetch --all --prune

FAILED_STEP="校验指定 commit"
as_user git -C "$REPO_DIR" cat-file -e "${REF}^{commit}"

FAILED_STEP="切换到指定 commit"
log "切换到 $REF(detached HEAD,不 pull 分支)…"
as_user git -C "$REPO_DIR" checkout --detach "$REF"

FAILED_STEP="安装后端依赖"
log "安装 backend/requirements.txt …"
as_user "$VENV/bin/pip" install -r "$REPO_DIR/backend/requirements.txt"

if as_user "$VENV/bin/python" -c 'import openjiuwen' >/dev/null 2>&1 && [ -f "$AGENT_REQ" ]; then
  FAILED_STEP="安装 openJiuwen 依赖"
  log "检测到已安装 openjiuwen,安装 requirements-agent.txt …"
  as_user "$VENV/bin/pip" install -r "$AGENT_REQ"
else
  log "未检测到 openjiuwen,跳过 requirements-agent.txt。"
fi

FAILED_STEP="执行数据库迁移"
log "alembic upgrade head …"
( cd "$REPO_DIR" && as_user "$VENV/bin/python" -m alembic -c backend/alembic.ini upgrade head )

FAILED_STEP="校验迁移版本"
CURRENT="$( cd "$REPO_DIR" && as_user "$VENV/bin/python" -m alembic -c backend/alembic.ini current 2>/dev/null | head -n1 | awk '{print $1}' )"
HEAD_REV="$( cd "$REPO_DIR" && as_user "$VENV/bin/python" -m alembic -c backend/alembic.ini heads 2>/dev/null | head -n1 | awk '{print $1}' )"
if [ -z "$CURRENT" ] || [ "$CURRENT" != "$HEAD_REV" ]; then
  printf '[update] alembic current=%s,heads=%s —— 迁移没有到 head\n' "${CURRENT:-<空>}" "${HEAD_REV:-<空>}" >&2
  false
fi
log "迁移版本已到 head:$CURRENT"

FAILED_STEP="启动服务"
log "启动 $SERVICE …"
systemctl start "$SERVICE"

FAILED_STEP="等待 /ready"
log "等待 $API_URL/ready …"
ready=0
for _ in $(seq 1 30); do
  if curl -fsS "$API_URL/ready" >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  printf '[update] 服务在 60s 内没有通过 %s/ready\n' "$API_URL" >&2
  false
fi

NEW_COMMIT="$(git_c rev-parse HEAD)"
printf '[update] 成功: %s -> %s(服务已就绪)\n' "$OLD_COMMIT" "$NEW_COMMIT"
