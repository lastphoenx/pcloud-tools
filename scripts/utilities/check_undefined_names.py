#!/usr/bin/env python3
"""
check_undefined_names.py - findet Namen, die benutzt, aber NIRGENDS im Modul
definiert/gebunden/importiert sind (= genau die Fehlerklasse "Konstante beim
Ausbau geloescht, aber noch referenziert").

Nur Python-Standardlib (ast, builtins) - keine Installation noetig.

Methode (bewusst konservativ, ~0 False Positives fuer diese Fehlerklasse):
  - bekannt = builtins
              + alle Top-Level- UND lokalen Zuweisungen (Name im Store-Kontext)
              + alle Funktions-/Lambda-Argumente
              + alle Importe (inkl. as-Alias)
              + alle except-Handler-Namen
              + alle global/nonlocal-deklarierten Namen
              + Funktions-/Klassennamen
  - gemeldet = jeder Name im Load-Kontext, der nicht in 'bekannt' ist

Das findet "definiert es NIRGENDS" zuverlaessig. Es prueft NICHT Scope/
Use-before-assign (dafuer waere ein echter Linter noetig) - aber genau das
brauchen wir hier nicht.

Usage:
  python scripts/utilities/check_undefined_names.py DATEI.py [DATEI2.py ...]
Exit-Code 1, wenn Treffer gefunden wurden (CI-tauglich).
"""
import ast
import builtins
import sys


def collect_bound_names(tree: ast.AST) -> set:
    bound = set(dir(builtins))
    bound.update({"__file__", "__name__", "__doc__", "__builtins__",
                  "__spec__", "__loader__", "__package__"})
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            a = node.args
            for arg in (list(getattr(a, "posonlyargs", [])) + list(a.args)
                        + list(a.kwonlyargs)):
                bound.add(arg.arg)
            if a.vararg:
                bound.add(a.vararg.arg)
            if a.kwarg:
                bound.add(a.kwarg.arg)
        elif isinstance(node, ast.Lambda):
            a = node.args
            for arg in (list(getattr(a, "posonlyargs", [])) + list(a.args)
                        + list(a.kwonlyargs)):
                bound.add(arg.arg)
            if a.vararg:
                bound.add(a.vararg.arg)
            if a.kwarg:
                bound.add(a.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name
                bound.add(name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
    return bound


def check_file(path: str) -> int:
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=path)
    bound = collect_bound_names(tree)

    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in bound:
                seen.setdefault(node.id, node.lineno)

    if not seen:
        print(f"[ok] {path}: keine undefinierten Namen")
        return 0

    print(f"[FEHLER] {path}: {len(seen)} undefinierte(r) Name(n):")
    for name, lineno in sorted(seen.items(), key=lambda kv: kv[1]):
        print(f"  {path}:{lineno}: undefined name '{name}'")
    return 1


def main(argv) -> int:
    if len(argv) < 2:
        print("Usage: python check_undefined_names.py DATEI.py [DATEI2.py ...]")
        return 2
    rc = 0
    for path in argv[1:]:
        try:
            rc |= check_file(path)
        except SyntaxError as e:
            print(f"[FEHLER] {path}: SyntaxError: {e}")
            rc |= 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
