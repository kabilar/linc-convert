"""Create a navigation file and markdown files for API docs."""

from pathlib import Path

import mkdocs_gen_files

nav = mkdocs_gen_files.Nav()

root = Path(__file__).parent.parent.parent
src = root / "linc_convert"
print(root, src)
for path in sorted(src.rglob("*.py")):
    module_path = path.relative_to(src).with_suffix("")
    doc_path = "api" / path.relative_to(src).with_suffix(".md")
    full_doc_path = Path(root, "docs", doc_path)
    parts = tuple(module_path.parts)
    print(module_path,doc_path,full_doc_path,parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    elif parts[-1] == "__main__":
        continue

    if parts:
        nav[parts] = Path(doc_path).as_posix()
        with mkdocs_gen_files.open(full_doc_path, "w") as fd:
            ident = ".".join(parts)
            fd.write(f"::: linc_convert.{ident}")

        mkdocs_gen_files.set_edit_path(full_doc_path, path.relative_to(src))

with mkdocs_gen_files.open(src / "docs/navigation.md", "w") as nav_file:
    nav_file.writelines(nav.build_literate_nav())
