"""Build call graphs and dependency graphs from parsed C files."""

from __future__ import annotations

from collections import defaultdict


class CallGraphBuilder:
    """Builds function-level and file-level call/dependency graphs."""

    def build(self, parsed_files: dict[str, dict]) -> dict:
        """Build call graph from parsed file data.

        Returns dict with:
          - function_calls: {caller: [callees]}
          - reverse_calls: {callee: [callers]}
          - file_dependencies: {file: [included_files]}
          - function_to_file: {func_name: file_path}
          - cross_file_calls: [(caller_file, callee_file, func_name)]
          - strongly_connected: list of function clusters (SCCs with >1 member)
        """
        function_calls: dict[str, list[str]] = {}
        function_to_file: dict[str, str] = {}
        all_functions: set[str] = set()
        all_calls: dict[str, set[str]] = defaultdict(set)

        # Collect all function definitions and their calls
        for filepath, pf in parsed_files.items():
            for func in pf["functions"]:
                name = func["name"]
                all_functions.add(name)
                function_to_file[name] = filepath
                function_calls[name] = func["calls"]
                for callee in func["calls"]:
                    all_calls[name].add(callee)

        # Build reverse call graph
        reverse_calls: dict[str, list[str]] = defaultdict(list)
        for caller, callees in function_calls.items():
            for callee in callees:
                reverse_calls[callee].append(caller)

        # File-level dependencies from #include
        file_deps: dict[str, list[str]] = {}
        for filepath, pf in parsed_files.items():
            file_deps[filepath] = pf.get("includes", [])

        # Cross-file calls: caller in file A calls function defined in file B
        cross_file = []
        for caller, callees in function_calls.items():
            caller_file = function_to_file.get(caller)
            for callee in callees:
                callee_file = function_to_file.get(callee)
                if callee_file and caller_file and callee_file != caller_file:
                    cross_file.append((caller_file, callee_file, callee))

        # Find strongly connected components (mutual recursion / tight clusters)
        sccs = self._tarjan_scc(all_functions, all_calls)
        # Only keep non-trivial SCCs (more than 1 function)
        strongly_connected = [scc for scc in sccs if len(scc) > 1]

        # Compute connectivity metrics
        metrics = self._compute_metrics(function_calls, reverse_calls, all_functions)

        return {
            "function_calls": function_calls,
            "reverse_calls": dict(reverse_calls),
            "file_dependencies": file_deps,
            "function_to_file": function_to_file,
            "cross_file_calls": cross_file,
            "strongly_connected": strongly_connected,
            "metrics": metrics,
        }

    def _compute_metrics(
        self,
        function_calls: dict[str, list[str]],
        reverse_calls: dict[str, list[str]],
        all_functions: set[str],
    ) -> dict:
        """Compute graph metrics for module detection."""
        # Fan-out: how many functions does each function call
        fan_out = {f: len(function_calls.get(f, [])) for f in all_functions}

        # Fan-in: how many functions call each function
        fan_in = {f: len(reverse_calls.get(f, [])) for f in all_functions}

        # Root functions: called by no one (entry points / API)
        roots = [f for f in all_functions if fan_in.get(f, 0) == 0]

        # Leaf functions: call no one (utilities / primitives)
        leaves = [f for f in all_functions if fan_out.get(f, 0) == 0]

        # Hub functions: high fan-in AND fan-out (dispatchers, glue)
        hubs = [
            f for f in all_functions
            if fan_in.get(f, 0) >= 3 and fan_out.get(f, 0) >= 3
        ]

        return {
            "fan_out": fan_out,
            "fan_in": fan_in,
            "roots": sorted(roots),
            "leaves": sorted(leaves),
            "hubs": sorted(hubs),
            "total_edges": sum(len(v) for v in function_calls.values()),
        }

    def _tarjan_scc(
        self,
        nodes: set[str],
        edges: dict[str, set[str]],
    ) -> list[list[str]]:
        """Tarjan's algorithm for strongly connected components."""
        index_counter = [0]
        stack: list[str] = []
        lowlink: dict[str, int] = {}
        index: dict[str, int] = {}
        on_stack: set[str] = set()
        result: list[list[str]] = []

        def strongconnect(v: str):
            index[v] = index_counter[0]
            lowlink[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack.add(v)

            for w in edges.get(v, set()):
                if w not in nodes:
                    continue  # external call (e.g., libc)
                if w not in index:
                    strongconnect(w)
                    lowlink[v] = min(lowlink[v], lowlink[w])
                elif w in on_stack:
                    lowlink[v] = min(lowlink[v], index[w])

            if lowlink[v] == index[v]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    component.append(w)
                    if w == v:
                        break
                result.append(sorted(component))

        # Increase recursion limit for large codebases
        import sys
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(max(old_limit, len(nodes) + 1000))
        try:
            for v in sorted(nodes):
                if v not in index:
                    strongconnect(v)
        finally:
            sys.setrecursionlimit(old_limit)

        return result
