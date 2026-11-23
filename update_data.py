import xml.etree.ElementTree as ET
import argparse
import glob
import os
import re
import shutil

# ===== 修改线程数 =====
def update_threads(root, threads, target_tg=None):
    for tg in root.iter("ThreadGroup"):
        tg_name = tg.attrib.get("testname")
        if target_tg and tg_name != target_tg:
            continue
        num_threads = tg.find('stringProp[@name="ThreadGroup.num_threads"]')
        if num_threads is not None:
            print(f"线程数: {num_threads.text} → {threads}")
            num_threads.text = str(threads)

# ===== 修改压测时间 =====
def update_duration(root, duration, target_tg=None):
    for tg in root.iter("ThreadGroup"):
        tg_name = tg.attrib.get("testname")
        if target_tg and tg_name != target_tg:
            continue
        dur = tg.find('stringProp[@name="ThreadGroup.duration"]')
        if dur is not None:
            print(f"压测时间: {dur.text} → {duration}")
            dur.text = str(duration)

# ===== 更新JMX =====
def update_jmx(file_path, threads, duration, target_tg=None):
    tree = ET.parse(file_path)
    root = tree.getroot()

    update_threads(root, threads, target_tg)
    update_duration(root, duration, target_tg)

    tree.write(file_path, encoding="utf-8", xml_declaration=True)
    print("✅ JMX更新完成")

# ===== 修改 run_*.sh 容器名 =====
def update_container(script_path, new_container, backup=True):
    if os.path.isdir(script_path):
        files = glob.glob(os.path.join(script_path, "run_*.sh"))
    else:
        files = [script_path]

    if not files:
        print("⚠️ 没有找到 run_*.sh 文件")
        return

    # 👉 支持带路径的 get_vram_*.sh
    pattern = re.compile(r'((?:\S*/)?get_vram_\w+\.sh\s+)(\S+)')

    for file in files:
        # ===== 备份 =====
        if backup:
            shutil.copy(file, file + ".bak")

        with open(file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = []
        for line in lines:
            if line.strip().startswith("#"):
                new_lines.append(line)
                continue

            def replace(match):
                old_container = match.group(2)

                # 如果已经是目标值就不改
                if old_container == new_container:
                    return match.group(0)

                print(f"[{file}] 容器名: {old_container} → {new_container}")
                return match.group(1) + new_container

            new_line = pattern.sub(replace, line, count=1)  # 只替换一次
            new_lines.append(new_line)

        with open(file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

    print("✅ run_*.sh 修改完成")

# ===== 主函数 =====
def main():
    parser = argparse.ArgumentParser(description="自动更新JMX线程数/压测时间/容器名")

    parser.add_argument("jmx_file", type=str, help="JMX文件路径")
    parser.add_argument("threads", type=int, help="线程数")
    parser.add_argument("duration", type=int, help="压测时间（秒）")

    parser.add_argument("--threadgroup", type=str, default=None,
                        help="指定线程组名，不填则更新所有线程组")

    parser.add_argument("--run-script", type=str,
                        help="run_*.sh 文件或目录路径")

    parser.add_argument("--container", type=str,
                        help="新的容器名")

    args = parser.parse_args()

    # ===== 修改JMX =====
    update_jmx(args.jmx_file, args.threads, args.duration, args.threadgroup)

    # ===== 修改脚本 =====
    if args.run_script and args.container:
        update_container(args.run_script, args.container)

if __name__ == "__main__":
    main()