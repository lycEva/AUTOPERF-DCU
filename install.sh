#!/usr/bin/env bash
# 把 dcu-bench 安装成任意目录都能调用的命令（默认 /usr/local/bin/dcu-bench 软链）。
#
# 用法:
#   bash install.sh                     # 软链到 /usr/local/bin
#   bash install.sh --prefix ~/.local/bin
#   bash install.sh --with-completion   # 同时装 bash 补全
#   bash install.sh --uninstall         # 移除软链（只删指向本项目的那个）
#
# 用软链而不是拷贝：以后更新项目里的脚本，命令自动跟着生效。
set -euo pipefail

SRC_DIR=$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)
SRC="$SRC_DIR/bin/dcu-bench"
NAME=dcu-bench
PREFIX=/usr/local/bin
COMPLETION_DIR=/etc/bash_completion.d
DO_UNINSTALL=0
WITH_COMPLETION=0
FORCE=0

usage() {
  cat <<'EOF'
用法: bash install.sh [选项]

  --prefix <目录>      安装目录，默认为 /usr/local/bin
  --with-completion    同时安装 bash 补全（/etc/bash_completion.d/dcu-bench）
  --uninstall          删除已安装的软链（只删指向本项目的那个）
  --force              覆盖同名普通文件 / 删除不是本项目的软链
  -h, --help           显示帮助
EOF
}

while [ "$#" -gt 0 ]; do
  case $1 in
    --prefix)
      [ "$#" -ge 2 ] || { echo "❌ --prefix 缺少参数" >&2; exit 2; }
      PREFIX=$2; shift 2 ;;
    --prefix=*)
      PREFIX=${1#*=}; shift ;;
    --with-completion) WITH_COMPLETION=1; shift ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    --force) FORCE=1; shift ;;
    -h | --help) usage; exit 0 ;;
    *) echo "❌ 未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
done

TARGET="$PREFIX/$NAME"

if [ "$DO_UNINSTALL" -eq 1 ]; then
  if [ -L "$TARGET" ]; then
    current=$(readlink -- "$TARGET")
    if [ "$current" = "$SRC" ] || [ "$FORCE" -eq 1 ]; then
      rm -f -- "$TARGET"
      echo "✅ 已删除 $TARGET"
    else
      echo "❌ $TARGET 指向 $current，不是本项目安装的；确认要删请加 --force" >&2
      exit 1
    fi
  elif [ -e "$TARGET" ]; then
    if [ "$FORCE" -eq 1 ]; then
      rm -rf -- "$TARGET"
      echo "✅ 已删除 $TARGET"
    else
      echo "❌ $TARGET 是普通文件/目录，不是本项目安装的软链；确认要删请加 --force" >&2
      exit 1
    fi
  else
    echo "ℹ️  $TARGET 不存在，无需卸载"
  fi
  completion_target="$COMPLETION_DIR/$NAME"
  completion_src="$SRC_DIR/completions/$NAME.bash"
  if [ -e "$completion_target" ]; then
    if [ "$FORCE" -eq 1 ] || grep -qF -- "installed-by-dcu-bench-installer: $SRC_DIR" "$completion_target" 2>/dev/null; then
      rm -f -- "$completion_target"
      echo "✅ 已删除补全文件 $completion_target"
    else
      echo "⚠️  保留 $completion_target：它不是本项目安装的补全文件（确认要删加 --force）" >&2
    fi
  fi
  exit 0
fi

[ -f "$SRC" ] || { echo "❌ 找不到 $SRC" >&2; exit 1; }
chmod +x "$SRC" 2>/dev/null || true

if ! mkdir -p -- "$PREFIX" 2>/dev/null; then
  echo "❌ 无法创建安装目录 $PREFIX（权限不足？试试 sudo 或 --prefix ~/.local/bin）" >&2
  exit 1
fi

if [ -d "$TARGET" ] && [ ! -L "$TARGET" ]; then
  echo "❌ $TARGET 是一个目录，无法安装软链；请先移走它或换 --prefix" >&2
  exit 1
fi
if [ -e "$TARGET" ] && [ ! -L "$TARGET" ] && [ "$FORCE" -ne 1 ]; then
  echo "❌ $TARGET 已存在且不是软链；确认覆盖请加 --force" >&2
  exit 1
fi
if [ -L "$TARGET" ]; then
  current=$(readlink -- "$TARGET")
  if [ "$current" != "$SRC" ] && [ "$FORCE" -ne 1 ]; then
    echo "❌ $TARGET 已指向 $current；确认覆盖请加 --force" >&2
    exit 1
  fi
fi

ln -sfn -- "$SRC" "$TARGET"
echo "✅ 已安装: $TARGET -> $SRC"

if [ "$WITH_COMPLETION" -eq 1 ]; then
  completion_src="$SRC_DIR/completions/$NAME.bash"
  if [ -f "$completion_src" ] && mkdir -p -- "$COMPLETION_DIR" 2>/dev/null; then
    # 追加来源标记，便于 --uninstall 只删自己装的那份，不误伤同名文件。
    {
      cat -- "$completion_src"
      printf '\n# installed-by-dcu-bench-installer: %s\n' "$SRC_DIR"
    } > "$COMPLETION_DIR/$NAME"
    echo "✅ 已安装 bash 补全: $COMPLETION_DIR/$NAME（新开终端生效）"
  else
    echo "⚠️  跳过补全安装（缺少 $completion_src 或无权限写 $COMPLETION_DIR）" >&2
  fi
fi

echo
echo "自检:"
"$TARGET" version || { echo "❌ 安装后自检失败" >&2; exit 1; }

case ":$PATH:" in
  *":$PREFIX:"*) echo "✅ $PREFIX 已在 PATH 中，任意目录直接执行: $NAME help" ;;
  *)
    echo "⚠️  $PREFIX 不在 PATH 中，请把下面一行加到 ~/.bashrc："
    echo "    export PATH=\"$PREFIX:\$PATH\""
    ;;
esac
