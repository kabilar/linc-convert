"""Create a navigation file and markdown files for API docs."""

from pathlib import Path

import mkdocs_gen_files

nav = mkdocs_gen_files.Nav()

# root = Path(__file__).parent.parent.parent
# src = root / "linc_convert"
src = Path('linc_convert')

# Generate API documentation for all Python modules
for path in sorted(src.rglob("*.py")):
    # Skip files that shouldn't be documented
    # if path.name.startswith("_") and path.name != "__init__.py":
        # continue
    
    doc_path = "api" / path.relative_to(src).with_suffix(".md")
    full_doc_path = Path(root, "docs", doc_path)
    module_path = path.relative_to(src).with_suffix("")
    parts = tuple(module_path.parts)

    if parts[-1] == "__init__":
        parts = parts[:-1]
    elif parts[-1] == "__main__":
        continue

    if parts:
        parts_modified = ('API',) + parts
        nav[parts_modified] = Path(doc_path).as_posix()
        
        with mkdocs_gen_files.open(full_doc_path, "w") as fd:
            ident = ".".join(parts)
            # Add a title and module documentation
            fd.write(f"# {ident}\n\n")
            fd.write(f"::: linc_convert.{ident}")

        mkdocs_gen_files.set_edit_path(full_doc_path, path.relative_to(root))

# Generate the navigation file
with mkdocs_gen_files.open(root / "docs/api.md", "w") as nav_file:
    nav_file.writelines(nav.build_literate_nav())
