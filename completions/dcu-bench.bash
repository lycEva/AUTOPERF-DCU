# dcu-bench 的 bash 补全。
# 安装：bash install.sh --with-completion
# 或临时启用：source completions/dcu-bench.bash
_dcu_bench() {
  local cur prev
  cur=${COMP_WORDS[COMP_CWORD]}
  prev=${COMP_WORDS[COMP_CWORD-1]}

  local subcommands="run config collect plot ports doctor version help"
  local run_opts="--service --port --ports-config --warmup --monitor-containers
    --monitor-interval --monitor-timeout --monitor-policy
    --monitor-max-consecutive-failures --monitor-max-failure-percent
    --server-config --workers --server-threads --target-path --config-wait
    --monitor-mode --help"

  case $prev in
    --ports-config | --server-config | --monitor-containers)
      compopt -o default 2>/dev/null
      COMPREPLY=()
      return 0
      ;;
    --monitor-policy)
      COMPREPLY=($(compgen -W "tolerant strict" -- "$cur"))
      return 0
      ;;
  esac

  # 第一个参数：补子命令
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "$subcommands" -- "$cur"))
    return 0
  fi

  case ${COMP_WORDS[1]} in
    ports)
      COMPREPLY=($(compgen -W "--ports-config --json --help" -- "$cur"))
      ;;
    doctor | version | help)
      COMPREPLY=()
      ;;
    *)
      # run/config/collect/plot 以及 dcu-bench <jmx> ... 老用法：选项名或文件路径
      if [[ $cur == -* ]]; then
        COMPREPLY=($(compgen -W "$run_opts" -- "$cur"))
      else
        compopt -o default 2>/dev/null
        COMPREPLY=()
      fi
      ;;
  esac
}

complete -F _dcu_bench dcu-bench
