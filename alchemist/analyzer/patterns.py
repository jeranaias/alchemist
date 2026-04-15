"""Algorithm pattern signatures for module classification.

Patterns match against function names, called functions, local variables,
and parameter types to classify C code into categories:
  - algorithm: mathematical/logical algorithms (compression, crypto, filters, etc.)
  - data_structure: data structure implementations (hash tables, trees, queues)
  - glue: infrastructure code (I/O, error handling, memory management)
  - platform: OS/hardware abstraction
  - api: public API surface, wrappers
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class AlgorithmPattern:
    """A pattern that identifies a category of code."""
    name: str
    category: str
    # Any of these in the function name → match
    name_keywords: list[str]
    # Any of these in called functions → boost
    call_keywords: list[str]
    # Any of these in local variable names → boost
    var_keywords: list[str]
    # Base confidence when name matches
    confidence: float = 0.7


ALGORITHM_PATTERNS: list[AlgorithmPattern] = [
    # ── Compression ──────────────────────────────────────────────────
    AlgorithmPattern(
        name="deflate_compression",
        category="algorithm",
        name_keywords=["deflate", "compress", "lz77", "lzss", "lzma", "lz4"],
        call_keywords=["longest_match", "fill_window", "flush_block", "insert_string",
                        "send_bits", "bi_flush", "ct_tally", "tr_tally"],
        var_keywords=["match_length", "hash_head", "prev_match", "window", "w_size",
                       "match_start", "strstart", "lookahead"],
        confidence=0.9,
    ),
    AlgorithmPattern(
        name="inflate_decompression",
        category="algorithm",
        name_keywords=["inflate", "decompress", "uncompress", "gunzip", "unzip"],
        call_keywords=["inflate_table", "inflate_fast", "updatewindow"],
        var_keywords=["hold", "bits", "have", "next", "window", "wsize", "wnext",
                       "codes", "lencode", "distcode"],
        confidence=0.9,
    ),
    AlgorithmPattern(
        name="huffman_coding",
        category="algorithm",
        name_keywords=["huffman", "huff", "tree_build", "gen_codes", "build_tree",
                        "tr_static_init", "init_block", "build_bl_tree",
                        "scan_tree", "send_tree", "send_all_trees", "compress_block",
                        "pqdownheap", "gen_bitlen"],
        call_keywords=["pqdownheap", "gen_bitlen", "gen_codes", "send_bits"],
        var_keywords=["bl_count", "next_code", "tree", "dyn_ltree", "dyn_dtree",
                       "bl_tree", "heap", "depth", "opt_len", "static_len"],
        confidence=0.9,
    ),

    # ── Checksums ────────────────────────────────────────────────────
    AlgorithmPattern(
        name="checksum_crc",
        category="algorithm",
        name_keywords=["crc32", "crc_", "crc16", "crc_combine", "crc_table",
                        "make_crc_table", "multmodp"],
        call_keywords=["crc32", "crc_combine", "multmodp", "x2nmodp"],
        var_keywords=["crc", "crc_table", "polynomial", "braid"],
        confidence=0.9,
    ),
    AlgorithmPattern(
        name="checksum_adler",
        category="algorithm",
        name_keywords=["adler32", "adler_combine"],
        call_keywords=["adler32"],
        var_keywords=["adler", "sum2"],
        confidence=0.9,
    ),

    # ── Cryptography ─────────────────────────────────────────────────
    AlgorithmPattern(
        name="block_cipher",
        category="algorithm",
        name_keywords=["aes", "des", "blowfish", "camellia", "aria",
                        "encrypt", "decrypt", "cipher"],
        call_keywords=["key_schedule", "add_round_key", "sub_bytes", "shift_rows",
                        "mix_columns", "sbox"],
        var_keywords=["sbox", "round_key", "rk", "nr", "key_len", "iv",
                       "plaintext", "ciphertext"],
        confidence=0.85,
    ),
    AlgorithmPattern(
        name="hash_function",
        category="algorithm",
        name_keywords=["sha256", "sha512", "sha1", "md5", "sha_", "blake",
                        "keccak", "sha3"],
        call_keywords=["sha256_transform", "sha_update", "sha_final", "md5_transform"],
        var_keywords=["digest", "hash_state", "msg_schedule", "block_size",
                       "h0", "h1", "h2", "h3"],
        confidence=0.85,
    ),
    AlgorithmPattern(
        name="public_key",
        category="algorithm",
        name_keywords=["rsa", "ecdsa", "ecdh", "dh_", "dsa_", "ed25519",
                        "x25519", "bignum", "mpi_"],
        call_keywords=["mpi_mul", "mpi_mod", "mpi_inv", "point_mul",
                        "mod_exp", "gcd"],
        var_keywords=["modulus", "exponent", "prime", "generator",
                       "pub_key", "priv_key"],
        confidence=0.85,
    ),

    # ── Signal Processing / Filters ──────────────────────────────────
    AlgorithmPattern(
        name="kalman_filter",
        category="algorithm",
        name_keywords=["ekf", "kalman", "predict", "fuse", "covariance"],
        call_keywords=["predict", "update", "fuse", "innovation"],
        var_keywords=["P", "Q", "R", "K", "x_hat", "state_vector",
                       "covariance", "innovation", "kalman_gain"],
        confidence=0.85,
    ),
    AlgorithmPattern(
        name="pid_controller",
        category="algorithm",
        name_keywords=["pid", "controller"],
        call_keywords=[],
        var_keywords=["kp", "ki", "kd", "integral", "derivative",
                       "setpoint", "error", "dt"],
        confidence=0.8,
    ),
    AlgorithmPattern(
        name="fft",
        category="algorithm",
        name_keywords=["fft", "dft", "ifft", "dct", "idct", "rfft"],
        call_keywords=["butterfly", "bit_reverse"],
        var_keywords=["twiddle", "butterfly", "radix", "spectrum"],
        confidence=0.85,
    ),

    # ── Data Structures ──────────────────────────────────────────────
    AlgorithmPattern(
        name="hash_table",
        category="data_structure",
        name_keywords=["hash_", "htable", "dict_", "map_"],
        call_keywords=["hash_func", "resize", "rehash"],
        var_keywords=["bucket", "hash_size", "load_factor", "collision"],
        confidence=0.7,
    ),
    AlgorithmPattern(
        name="tree_structure",
        category="data_structure",
        name_keywords=["btree", "rbtree", "avl", "bst_", "tree_insert",
                        "tree_delete", "tree_search"],
        call_keywords=["rotate_left", "rotate_right", "rebalance", "split_node"],
        var_keywords=["left", "right", "parent", "color", "height", "root"],
        confidence=0.7,
    ),
    AlgorithmPattern(
        name="queue_buffer",
        category="data_structure",
        name_keywords=["queue_", "ring_buf", "fifo", "circular"],
        call_keywords=[],
        var_keywords=["head", "tail", "capacity", "front", "rear"],
        confidence=0.7,
    ),

    # ── Protocol / State Machine ─────────────────────────────────────
    AlgorithmPattern(
        name="protocol_parser",
        category="algorithm",
        name_keywords=["parse", "decode", "encode", "serialize", "deserialize",
                        "packet", "frame", "handshake"],
        call_keywords=["read_byte", "write_byte", "get_bits", "put_bits"],
        var_keywords=["state", "next_state", "opcode", "payload", "header",
                       "sequence", "length"],
        confidence=0.6,
    ),
    AlgorithmPattern(
        name="tcp_ip",
        category="algorithm",
        name_keywords=["tcp_", "udp_", "ip_", "icmp_", "arp_", "dhcp_", "dns_"],
        call_keywords=["checksum", "send_packet", "recv_packet"],
        var_keywords=["seq", "ack", "window", "mss", "ttl", "src_addr", "dst_addr"],
        confidence=0.8,
    ),

    # ── RTOS / Scheduler ─────────────────────────────────────────────
    AlgorithmPattern(
        name="scheduler",
        category="algorithm",
        name_keywords=["schedule", "task_create", "task_switch", "context_switch",
                        "yield", "dispatch"],
        call_keywords=["context_save", "context_restore", "pendsv"],
        var_keywords=["priority", "ready_list", "current_task", "stack_ptr",
                       "tick_count"],
        confidence=0.8,
    ),

    # ── Glue / Infrastructure ────────────────────────────────────────
    AlgorithmPattern(
        name="memory_management",
        category="glue",
        name_keywords=["alloc", "malloc", "calloc", "realloc", "free",
                        "mem_init", "mem_pool"],
        call_keywords=["malloc", "calloc", "free", "realloc", "memcpy", "memset"],
        var_keywords=["pool", "block_size", "heap"],
        confidence=0.7,
    ),
    AlgorithmPattern(
        name="io_operations",
        category="glue",
        name_keywords=["read", "write", "open", "close", "seek", "tell",
                        "flush", "fread", "fwrite", "gz"],
        call_keywords=["fopen", "fclose", "fread", "fwrite", "fseek", "ftell",
                        "read", "write", "lseek"],
        var_keywords=["fd", "fp", "stream", "buf", "nbytes", "offset"],
        confidence=0.6,
    ),
    AlgorithmPattern(
        name="error_handling",
        category="glue",
        name_keywords=["error", "err_", "strerror", "perror"],
        call_keywords=["fprintf", "perror", "strerror", "abort", "exit"],
        var_keywords=["errno", "err_code", "err_msg"],
        confidence=0.6,
    ),
    AlgorithmPattern(
        name="platform_abstraction",
        category="platform",
        name_keywords=["os_", "sys_", "port_", "hal_", "bsp_", "arch_"],
        call_keywords=["ioctl", "mmap", "sysconf"],
        var_keywords=[],
        confidence=0.7,
    ),
]


def classify_function(
    name: str,
    calls: list[str],
    local_vars: list[str],
    params: list[dict],
) -> tuple[str, str | None, float]:
    """Classify a function by matching against known patterns.

    Returns (category, pattern_name, confidence).
    """
    name_lower = name.lower()
    calls_lower = {c.lower() for c in calls}
    vars_lower = {v.lower() for v in local_vars}

    best_category = "glue"
    best_pattern = None
    best_score = 0.0

    for pattern in ALGORITHM_PATTERNS:
        score = 0.0

        # Name match (strongest signal)
        for kw in pattern.name_keywords:
            if kw in name_lower:
                score += pattern.confidence
                break

        # Call match
        for kw in pattern.call_keywords:
            if kw in calls_lower:
                score += 0.15
                break

        # Variable match
        matched_vars = sum(1 for kw in pattern.var_keywords if kw in vars_lower)
        if matched_vars >= 2:
            score += 0.2
        elif matched_vars == 1:
            score += 0.1

        if score > best_score:
            best_score = score
            best_category = pattern.category
            best_pattern = pattern.name

    # Normalize confidence to [0, 1]
    confidence = min(best_score, 1.0)

    # If no pattern matched well, default to glue with low confidence
    if best_score < 0.3:
        return ("glue", None, 0.1)

    return (best_category, best_pattern, confidence)
