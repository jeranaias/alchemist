"""Detect and classify algorithmic modules within a C codebase.

Uses call graph clustering, filename heuristics, and pattern matching
to identify modules and classify them as algorithm / data_structure /
glue / platform / api.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from alchemist.analyzer.patterns import ALGORITHM_PATTERNS, classify_function


class ModuleDetector:
    """Detect algorithmic modules from parsed files and call graph."""

    def detect(self, parsed_files: dict[str, dict], call_graph: dict) -> list[dict]:
        """Detect modules and return a list of module descriptors.

        Each module has: name, category, functions, files, total_lines, confidence,
        description.
        """
        # Step 1: Group functions by file (natural module boundary in C)
        file_modules = self._group_by_file(parsed_files)

        # Step 2: Classify each file-module
        modules = []
        for filepath, file_data in file_modules.items():
            module = self._classify_file_module(filepath, file_data, call_graph)
            if module:
                modules.append(module)

        # Step 3: Merge tightly coupled modules (e.g., inflate + inffast + inftrees)
        modules = self._merge_coupled_modules(modules, call_graph)

        # Step 4: Sort by importance (algorithmic first, then by size)
        category_order = {
            "algorithm": 0,
            "data_structure": 1,
            "api": 2,
            "glue": 3,
            "platform": 4,
        }
        modules.sort(key=lambda m: (category_order.get(m["category"], 5), -m["total_lines"]))

        return modules

    def _group_by_file(self, parsed_files: dict[str, dict]) -> dict[str, dict]:
        """Group parsed data by source file (only .c files, skip .h)."""
        groups = {}
        headers = {}

        for filepath, pf in parsed_files.items():
            if filepath.endswith(".h"):
                headers[filepath] = pf
            else:
                groups[filepath] = pf

        # Associate headers with their .c files
        for filepath, pf in groups.items():
            stem = Path(filepath).stem
            associated_headers = [
                h for h in headers
                if Path(h).stem == stem or Path(h).stem in (f"{stem}_internal", f"{stem}s")
            ]
            pf["_associated_headers"] = associated_headers
            # Merge struct/typedef info from headers
            for hpath in associated_headers:
                hdata = headers[hpath]
                pf["structs"] = pf.get("structs", []) + hdata.get("structs", [])
                pf["typedefs"] = {**pf.get("typedefs", {}), **hdata.get("typedefs", {})}
                pf["macros"] = pf.get("macros", []) + hdata.get("macros", [])

        return groups

    def _classify_file_module(
        self,
        filepath: str,
        file_data: dict,
        call_graph: dict,
    ) -> dict | None:
        """Classify a single file as a module."""
        functions = file_data.get("functions", [])
        if not functions:
            return None

        filename = Path(filepath).stem.lower()
        func_names = [f["name"] for f in functions]

        # Classify each function using pattern matching
        func_classifications = {}
        for func in functions:
            category, pattern_name, confidence = classify_function(
                func["name"],
                func.get("calls", []),
                func.get("local_vars", []),
                func.get("params", []),
            )
            func_classifications[func["name"]] = (category, pattern_name, confidence)

        # Aggregate: module category is the majority classification
        category_counts = defaultdict(float)
        for name, (cat, pat, conf) in func_classifications.items():
            # Weight by function size
            func_obj = next((f for f in functions if f["name"] == name), None)
            weight = func_obj["line_count"] if func_obj else 1
            category_counts[cat] += weight * conf

        # Also apply filename-level heuristics
        file_category = self._classify_by_filename(filename)
        if file_category:
            category_counts[file_category] += 100  # strong signal

        # Pick dominant category
        if not category_counts:
            module_category = "glue"
        else:
            module_category = max(category_counts, key=category_counts.get)

        total_lines = sum(f["line_count"] for f in functions)

        # Build description
        pattern_names = set()
        for name, (cat, pat, conf) in func_classifications.items():
            if pat and conf > 0.3:
                pattern_names.add(pat)

        description = f"File-level module from {Path(filepath).name}"
        if pattern_names:
            description += f" — detected patterns: {', '.join(sorted(pattern_names))}"

        return {
            "name": filename,
            "category": module_category,
            "functions": func_names,
            "function_details": func_classifications,
            "files": [filepath] + file_data.get("_associated_headers", []),
            "total_lines": total_lines,
            "confidence": min(max(category_counts.values()) / (total_lines + 1), 1.0) if category_counts else 0.0,
            "description": description,
            "structs": [s["name"] for s in file_data.get("structs", [])],
            "macros": [m["name"] for m in file_data.get("macros", [])],
        }

    def _classify_by_filename(self, filename: str) -> str | None:
        """Heuristic classification based on filename."""
        algo_names = {
            "deflate", "inflate", "compress", "decompress",
            "encrypt", "decrypt", "cipher", "aes", "sha", "rsa", "ecdsa", "hmac",
            "crc", "crc32", "adler", "adler32", "checksum", "hash",
            "ekf", "kalman", "filter", "pid",
            "fft", "dft", "dct",
            "sort", "search", "btree", "rbtree", "avl",
            "huffman", "lz77", "lzma", "bzip",
            "tcp", "udp", "ip", "arp", "dhcp", "dns",
            "scheduler", "queue", "semaphore", "mutex",
            "trees",  # zlib: Huffman tree construction
            "inffast", "inftrees", "infback",  # zlib inflate components
            "miniz",  # miniz: single-file zlib-compatible compression
            "tdefl", "tinfl",  # miniz's internal encode/decode modules
        }
        glue_names = {
            "util", "utils", "helper", "helpers", "compat",
            "platform", "port", "sys", "os",
            "log", "logging", "debug", "trace",
            "error", "err",
            "main",
        }
        io_names = {
            "read", "write", "io", "file", "stream", "buf", "buffer",
            "gzread", "gzwrite", "gzlib", "gzclose",
        }
        api_names = {
            "api", "interface", "public", "export",
        }

        if filename in algo_names:
            return "algorithm"
        if filename in glue_names:
            return "glue"
        if filename in io_names:
            return "glue"
        if filename in api_names:
            return "api"

        # Partial matches
        for algo in algo_names:
            if algo in filename:
                return "algorithm"

        return None

    def _merge_coupled_modules(
        self,
        modules: list[dict],
        call_graph: dict,
    ) -> list[dict]:
        """Merge modules that are tightly coupled via cross-file calls.

        E.g., inflate + inffast + inftrees → single 'inflate' module.
        """
        if len(modules) <= 1:
            return modules

        # Build module-to-module coupling strength
        func_to_module = {}
        for mod in modules:
            for func in mod["functions"]:
                func_to_module[func] = mod["name"]

        coupling: dict[tuple[str, str], int] = defaultdict(int)
        for caller, callees in call_graph.get("function_calls", {}).items():
            caller_mod = func_to_module.get(caller)
            if not caller_mod:
                continue
            for callee in callees:
                callee_mod = func_to_module.get(callee)
                if callee_mod and callee_mod != caller_mod:
                    pair = tuple(sorted([caller_mod, callee_mod]))
                    coupling[pair] += 1

        # Merge pairs with strong coupling (>= 3 cross-calls)
        # Use known merge groups first
        merge_groups = [
            {"inflate", "inffast", "inftrees", "infback"},  # zlib inflate family
        ]

        merged_names = set()
        result = []

        for group in merge_groups:
            group_modules = [m for m in modules if m["name"] in group]
            if len(group_modules) >= 2:
                merged = self._merge_module_list(group_modules)
                result.append(merged)
                merged_names.update(m["name"] for m in group_modules)

        # Also merge any dynamically detected high-coupling pairs
        for (m1, m2), count in coupling.items():
            if count >= 5 and m1 not in merged_names and m2 not in merged_names:
                mod1 = next((m for m in modules if m["name"] == m1), None)
                mod2 = next((m for m in modules if m["name"] == m2), None)
                if mod1 and mod2:
                    merged = self._merge_module_list([mod1, mod2])
                    result.append(merged)
                    merged_names.add(m1)
                    merged_names.add(m2)

        # Add unmerged modules
        for mod in modules:
            if mod["name"] not in merged_names:
                result.append(mod)

        return result

    def _merge_module_list(self, modules: list[dict]) -> dict:
        """Merge multiple modules into one."""
        # Prefer well-known primary names over just largest
        _preferred = {"inflate", "deflate", "tcp", "tls", "scheduler", "crypto"}
        primary = None
        for mod in modules:
            if mod["name"] in _preferred:
                primary = mod
                break
        if not primary:
            primary = max(modules, key=lambda m: m["total_lines"])
        all_funcs = []
        all_files = []
        total_lines = 0
        all_structs = []
        all_macros = []
        all_details = {}

        for mod in modules:
            all_funcs.extend(mod["functions"])
            all_files.extend(mod["files"])
            total_lines += mod["total_lines"]
            all_structs.extend(mod.get("structs", []))
            all_macros.extend(mod.get("macros", []))
            all_details.update(mod.get("function_details", {}))

        component_names = sorted(m["name"] for m in modules if m["name"] != primary["name"])

        return {
            "name": primary["name"],
            "category": primary["category"],
            "functions": all_funcs,
            "function_details": all_details,
            "files": all_files,
            "total_lines": total_lines,
            "confidence": primary["confidence"],
            "description": f"Merged module: {primary['name']} + {', '.join(component_names)}",
            "structs": all_structs,
            "macros": all_macros,
        }
