import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class DiffAnalysis:
    is_big_change: bool
    reason: str
    affected_symbols: List[str]
    impacted_files: List[str]


def _get_assign_name(target) -> Optional[str]:
    if isinstance(target, ast.Name):
        return target.id
    elif isinstance(target, ast.Attribute):
        return target.attr
    return None


def _ast_value_to_str(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def extract_changed_symbols(old_code: str, new_code: str) -> List[str]:
    changed: List[str] = []

    def parse(source: str) -> Optional[ast.Module]:
        try:
            return ast.parse(source)
        except SyntaxError:
            return None

    old_tree = parse(old_code)
    new_tree = parse(new_code)

    if not old_tree or not new_tree:
        return changed

    def get_func_sigs(tree: ast.Module) -> Dict[str, str]:
        sigs: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                sigs[node.name] = f"{node.name}({', '.join(args)})"
        return sigs

    old_funcs = get_func_sigs(old_tree)
    new_funcs = get_func_sigs(new_tree)

    for name, sig in new_funcs.items():
        if name in old_funcs and old_funcs[name] != sig:
            changed.append(name)

    def get_class_vars(tree: ast.Module) -> Dict[str, Dict[str, str]]:
        result: Dict[str, Dict[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                vars: Dict[str, str] = {}
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            name = _get_assign_name(target)
                            if name:
                                vars[name] = _ast_value_to_str(item.value)
                    elif isinstance(item, ast.AnnAssign) and item.value:
                        name = _get_assign_name(item.target)
                        if name:
                            vars[name] = _ast_value_to_str(item.value)
                result[node.name] = vars
        return result

    old_class_vars = get_class_vars(old_tree)
    new_class_vars = get_class_vars(new_tree)

    for class_name, new_vars in new_class_vars.items():
        old_vars = old_class_vars.get(class_name, {})
        all_var_names = set(old_vars) | set(new_vars)
        for var_name in all_var_names:
            symbol = f"{class_name}.{var_name}"
            if var_name not in old_vars:
                changed.append(symbol)
            elif var_name not in new_vars:
                changed.append(symbol)
            elif old_vars[var_name] != new_vars[var_name]:
                changed.append(symbol)

    def get_module_constants(tree: ast.Module) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    name = _get_assign_name(target)
                    if name and name.isupper():
                        result[name] = _ast_value_to_str(node.value)
            elif isinstance(node, ast.AnnAssign) and node.value:
                name = _get_assign_name(node.target)
                if name and name.isupper():
                    result[name] = _ast_value_to_str(node.value)
        return result

    old_consts = get_module_constants(old_tree)
    new_consts = get_module_constants(new_tree)

    all_const_names = set(old_consts) | set(new_consts)
    for name in all_const_names:
        if name not in old_consts:
            changed.append(name)
        elif name not in new_consts:
            changed.append(name)
        elif old_consts[name] != new_consts[name]:
            changed.append(name)

    return changed


def _collect_aliases(tree: ast.Module) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value, ast.Name):
                    aliases[target.id] = node.value.id
        elif isinstance(node, ast.ImportFrom):
            imported: ast.alias
            for imported in node.names:
                if imported.asname and imported.name:
                    aliases[imported.asname] = imported.name

    return aliases


def find_usages_in_repo(
    symbols: List[str], repo_root: Path, exclude_file: str
) -> Dict[str, List[str]]:
    usages: Dict[str, List[str]] = {s: [] for s in symbols}

    qualified: Dict[str, tuple[str, str]] = {}
    plain: set[str] = set()
    for sym in symbols:
        if "." in sym:
            parts = sym.split(".", 1)
            qualified[sym] = (parts[0], parts[1])
        else:
            plain.add(sym)

    py_files = [
        p
        for p in repo_root.rglob("*.py")
        if ".venv" not in p.parts
        and "node_modules" not in p.parts
        and str(p) != str(repo_root / exclude_file)
    ]

    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue

        rel = str(py_file.relative_to(repo_root))

        aliases = _collect_aliases(tree)
        reverse_aliases: Dict[str, List[str]] = {}
        for alias_name, original in aliases.items():
            reverse_aliases.setdefault(original, []).append(alias_name)

        def record(sym: str):
            if rel not in usages[sym]:
                usages[sym].append(rel)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and plain:
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name and name in plain:
                    record(name)

            elif isinstance(node, ast.Attribute):
                attr_name = node.attr
                if isinstance(node.value, ast.Name):
                    obj_name = node.value.id
                    full = f"{obj_name}.{attr_name}"
                    if full in qualified:
                        record(full)
                    if obj_name in aliases:
                        original_class = aliases[obj_name]
                        full_via_alias = f"{original_class}.{attr_name}"
                        if full_via_alias in qualified:
                            record(full_via_alias)

            elif isinstance(node, ast.Name) and node.id in plain:
                record(node.id)

            elif isinstance(node, ast.ImportFrom):
                for imp in node.names:  # type: ignore[attr-defined]
                    imported = getattr(imp, "asname", None) or getattr(
                        imp, "name", None
                    )
                    if imported in plain:
                        record(imported)

    return usages


def collect_impacted_file_snippets(
    usages: dict[str, list[str]],
    repo_root: Path,
    symbols: list[str],
) -> str:
    snippets: list[str] = []

    flat_symbols = set()
    for sym in symbols:
        flat_symbols.add(sym)
        if "." in sym:
            flat_symbols.add(sym.split(".", 1)[1])

    all_files = {f for files in usages.values() for f in files}
    for file_path in all_files:
        full_path = repo_root / file_path
        try:
            source = full_path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
        except (SyntaxError, OSError):
            continue

        relevant_nodes = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                node_src = ast.get_source_segment(source, node)
                if node_src and any(sym in node_src for sym in flat_symbols):
                    relevant_nodes.append(node_src)

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            node_src = ast.get_source_segment(source, node)
            if node_src and any(sym in node_src for sym in flat_symbols):
                relevant_nodes.append(node_src)

        if relevant_nodes:
            snippets.append(f"\n### {file_path}\n" + "\n\n".join(relevant_nodes))

    return "\n".join(snippets)
