"""Sync .ipynb notebooks from .py source — update process_chapter + _report_chapter."""
import json
import sys

PY_PATH = r"H:\2026_main_project\ximalaya_manager\colab\ximalaya_colab_worker.py"
NOTEBOOKS = [
    r"H:\2026_main_project\ximalaya_manager\colab\ximalaya_colab_worker.ipynb",
    r"H:\2026_main_project\ximalaya_manager\colab\ximalaya_colab_worker_mt.ipynb",
]

with open(PY_PATH, "r", encoding="utf-8") as f:
    py_content = f.read()

# Block 1: process_chapter body from 'time.sleep(download_interval)' to end of finally block
start_marker = "            time.sleep(download_interval)"
end_marker = "            shutil.rmtree(tmp_dir, ignore_errors=True)"
start_idx = py_content.find(start_marker)
end_idx = py_content.find(end_marker)
end_idx = py_content.find("\n", end_idx + len(end_marker)) + 1
new_code_block = py_content[start_idx:end_idx]

# Block 2: _report_chapter method
rc_start = py_content.find("    def _report_chapter(self, job_id:")
rc_end_marker = '            return {"ok": False, "error": str(e)}'
rc_end_idx = py_content.find(rc_end_marker, rc_start)
rc_end_idx = py_content.find("\n", rc_end_idx + len(rc_end_marker)) + 1
new_rc_block = py_content[rc_start:rc_end_idx]

print(f"Block1 length: {len(new_code_block)}")
print(f"Block2 length: {len(new_rc_block)}")

for nb_path in NOTEBOOKS:
    with open(nb_path, "r", encoding="utf-8") as f:
        nb = json.load(f)

    changed = False
    for cell in nb["cells"]:
        if cell["cell_type"] != "code":
            continue
        src = "".join(cell["source"])

        # Replace Block 1: process_chapter body
        if "time.sleep(download_interval)" in src and "shutil.rmtree(tmp_dir" in src:
            old_start = src.find(start_marker)
            old_end = src.find(end_marker)
            old_end = src.find("\n", old_end + len(end_marker)) + 1
            old_block = src[old_start:old_end]
            if old_block != new_code_block:
                src = src[:old_start] + new_code_block + src[old_end:]
                cell["source"] = src.splitlines(keepends=True)
                changed = True
                print(f"  Updated process_chapter in {nb_path}")

        # Replace Block 2: _report_chapter
        if "def _report_chapter(self, job_id:" in src:
            old_rc_start = src.find("    def _report_chapter(self, job_id:")
            old_rc_end_marker = '            return {"ok": False, "error": str(e)}'
            old_rc_end = src.find(old_rc_end_marker, old_rc_start)
            if old_rc_end >= 0:
                old_rc_end = src.find("\n", old_rc_end + len(old_rc_end_marker)) + 1
                old_rc = src[old_rc_start:old_rc_end]
                if old_rc != new_rc_block:
                    src = src[:old_rc_start] + new_rc_block + src[old_rc_end:]
                    cell["source"] = src.splitlines(keepends=True)
                    changed = True
                    print(f"  Updated _report_chapter in {nb_path}")

    if changed:
        with open(nb_path, "w", encoding="utf-8") as f:
            json.dump(nb, f, ensure_ascii=False, indent=1)
        print(f"  Saved {nb_path}")
    else:
        print(f"  No changes needed in {nb_path}")

print("Done.")
