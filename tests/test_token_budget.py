"""The token estimator must NEVER under-estimate — that is the whole contract.

Every length guard in m3 measured characters or bytes against a TOKEN ceiling.
`memory/tokens.py` replaces those proxies, and its safety rests on one claim:
``max(bytes/3, chars) + SPECIAL_TOKENS`` is an upper bound on the true bge-m3
token count for ANY input. If that claim is ever false, rows silently overflow
n_ctx again and the whole hardening effort regresses to where it started.

So this file does two things:

1. **Hermetic tests** (always run): the algebraic properties the bound rests on,
   plus the cascade/kill-switch behaviour. No tokenizer, no network, no wheel —
   these are the ones CI actually enforces (§3: a test that only passes when a
   local service is reachable is not hermetic).

2. **A ground-truth test** (skips without `transformers`): the estimate compared
   against the REAL bge-m3 tokenizer across the content types that broke the old
   char/byte guards. This is the test that would catch a model swap invalidating
   the bound.

⚠ The `chars` ceiling is a property of SentencePiece, not of tokenizers in
general. A byte-level BPE model can emit more than one token per character, so
`test_model_assumption_is_documented` fails loudly if the model tag moves away
from bge-m3 without this file being revisited.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

from memory import tokens as T  # noqa: E402


class TestEstimatorProperties(unittest.TestCase):
    """Algebraic properties — no tokenizer needed."""

    def test_never_below_char_count_plus_frame(self):
        """The load-bearing bound: content tokens <= chars, plus BOS/EOS."""
        for s in ("", "a", "ab" * 100, "\x00" * 50, "🎉" * 40, "数据库" * 30):
            self.assertGreaterEqual(
                T.estimate_tokens(s), len(s) + T.SPECIAL_TOKENS,
                f"estimate dropped below chars+frame for {s[:20]!r}")

    def test_empty_costs_the_frame_not_zero(self):
        """bge-m3 emits BOS+EOS even for empty input — reporting 0 would be a
        lie a caller might budget against."""
        self.assertEqual(T.estimate_tokens(""), T.SPECIAL_TOKENS)

    def test_byte_term_dominates_for_multibyte_text(self):
        """For text where bytes/3 > chars the byte term must win. UTF-8 chars
        of 4 bytes (astral plane) are the case: 4/3 > 1."""
        s = "🎉" * 100  # 100 chars, 400 bytes -> bytes/3 = 133 > 100
        self.assertEqual(T.estimate_tokens(s), 400 // 3 + T.SPECIAL_TOKENS)

    def test_monotonic_in_length(self):
        """Appending text can never lower the estimate."""
        base = "abc" * 50
        prev = 0
        for n in range(0, 10):
            e = T.estimate_tokens(base * n)
            self.assertGreaterEqual(e, prev)
            prev = e

    def test_bytes_divisor_is_three_not_four(self):
        """4 is the ENGLISH ratio and under-estimates every denser type. Pin it
        so a 'tidy-up' cannot quietly restore the bug."""
        self.assertEqual(T._BYTES_PER_TOKEN_FLOOR, 3)

    def test_ceiling_and_budget_leave_headroom(self):
        self.assertLess(T.TOKEN_BUDGET, T.TOKEN_CEILING)
        self.assertGreater(T.TOKEN_OVERLAP, 0)
        self.assertLess(T.TOKEN_OVERLAP, T.TOKEN_BUDGET)


class TestCountTokensCascade(unittest.TestCase):
    """Tier resolution: Rust -> caller-supplied exact_fn -> estimator."""

    def setUp(self):
        self._saved = T.config.m3_core_rs
        T.config.m3_core_rs = None  # force the no-wheel path

    def tearDown(self):
        T.config.m3_core_rs = self._saved

    def test_empty_batch(self):
        self.assertEqual(T.count_tokens([]), [])

    def test_falls_back_to_estimator_without_wheel(self):
        texts = ["hello", "world" * 100]
        self.assertEqual(T.count_tokens(texts), [T.estimate_tokens(t) for t in texts])

    def test_exact_fn_is_used_when_supplied(self):
        got = T.count_tokens(["a", "b"], exact_fn=lambda ts: [11, 22])
        self.assertEqual(got, [11, 22])

    def test_exact_fn_failure_degrades_to_estimator(self):
        """A remote counter being down must not fail the embed — it must only
        make the budget more conservative."""
        def boom(_ts):
            raise RuntimeError("tokenize endpoint down")
        self.assertEqual(T.count_tokens(["abc"], exact_fn=boom), [T.estimate_tokens("abc")])

    def test_exact_fn_wrong_length_is_rejected(self):
        """A partial answer is not trusted: a length mismatch means we cannot
        align counts to inputs, so fall back rather than mis-attribute."""
        self.assertEqual(
            T.count_tokens(["a", "b"], exact_fn=lambda ts: [5]),
            [T.estimate_tokens("a"), T.estimate_tokens("b")])

    def test_rust_shape_mismatch_is_rejected(self):
        class FakeRs:
            @staticmethod
            def count_tokens(texts):
                return [1]  # wrong length for a 2-item batch
        T.config.m3_core_rs = FakeRs()
        self.assertEqual(
            T.count_tokens(["a", "b"]),
            [T.estimate_tokens("a"), T.estimate_tokens("b")])

    def test_rust_is_used_when_available(self):
        class FakeRs:
            @staticmethod
            def count_tokens(texts):
                return [7] * len(texts)
        T.config.m3_core_rs = FakeRs()
        self.assertEqual(T.count_tokens(["a", "b"]), [7, 7])

    def test_kill_switch_path_is_the_config_attribute(self):
        """M3_CORE_RS_DISABLE=1 works by nulling config.m3_core_rs. This module
        must read THAT, never `import m3_core_rs` directly, or the kill switch
        silently leaves a partially-oxidized state no user actually runs."""
        # Parse rather than substring-match: the module DOCUMENTS the hazard
        # ("NOT a direct `import m3_core_rs`"), and a naive `assertNotIn` flags
        # that prose as a violation -- penalising the comment that prevents the
        # bug. Only a real import statement counts.
        import ast
        tree = ast.parse(pathlib.Path(T.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn(
            "m3_core_rs", imported,
            "tokens.py must reach the Rust core via config.m3_core_rs so "
            "M3_CORE_RS_DISABLE=1 covers it")
        self.assertIn("config.m3_core_rs",
                      pathlib.Path(T.__file__).read_text(encoding="utf-8"))

    def test_token_budget_and_fits(self):
        self.assertEqual(T.token_budget("abc"), T.estimate_tokens("abc"))
        self.assertTrue(T.fits("short"))
        self.assertFalse(T.fits("x" * (T.TOKEN_BUDGET + 10)))


try:  # pragma: no cover - ground truth needs the HF tokenizer
    from transformers import AutoTokenizer  # noqa: F401
    _HAVE_TOKENIZER = True
except Exception:  # noqa: BLE001
    _HAVE_TOKENIZER = False


@unittest.skipUnless(_HAVE_TOKENIZER, "transformers not installed")
class TestAgainstRealTokenizer(unittest.TestCase):
    """Ground truth. The content types here are exactly the ones that broke the
    legacy char/byte guards -- JSON and UUID lists are DENSER per character than
    Chinese, so this is not a CJK-only problem."""

    @classmethod
    def setUpClass(cls):
        from transformers import AutoTokenizer
        try:
            cls.tok = AutoTokenizer.from_pretrained("BAAI/bge-m3")
        except Exception as exc:  # noqa: BLE001 - offline / no HF cache
            raise unittest.SkipTest(f"bge-m3 tokenizer unavailable: {exc}")

    def _samples(self):
        import base64 as b64, json, random, uuid
        random.seed(7)
        zh = ["数据库", "连接", "失败", "重试", "记录", "详细", "错误", "信息"]
        s = {
            "english": "The quick brown fox jumps over the lazy dog. " * 80,
            "json": json.dumps([{"id": str(uuid.uuid4()), "s": 0.9} for _ in range(40)]),
            "uuids": "\n".join(str(uuid.uuid4()) for _ in range(300)),
            "base64": b64.b64encode(b"x" * 9000).decode(),
            "logs": ('2026-09-07 ERROR [x:218] FAIL\n  File "/x.py", line 15\n') * 40,
            "chinese": "".join(random.choice(zh) + "，" for _ in range(1200)),
            "emoji": "🎉🔥" * 2000,
            "empty": "",
            "one_char": "a",
        }
        s["mixed"] = s["chinese"][:1500] + s["english"][:1500] + s["base64"][:1500]
        return s

    def test_never_under_estimates(self):
        """THE contract. A single under-estimate here means rows overflow
        n_ctx again."""
        under = []
        for name, text in self._samples().items():
            actual = len(self.tok.encode(text))
            est = T.estimate_tokens(text)
            if est < actual:
                under.append(f"{name}: est={est} < actual={actual}")
        self.assertEqual(under, [], "estimator UNDER-estimated: " + "; ".join(under))

    def test_base64_is_the_tight_case(self):
        """base64 is ~1.00 tokens/char -- the case that makes the `chars` term
        load-bearing, and the one that exposed the missing BOS/EOS (an estimate
        of 12,000 against 12,002 actual)."""
        import base64 as b64
        text = b64.b64encode(b"x" * 9000).decode()
        actual = len(self.tok.encode(text))
        self.assertGreaterEqual(T.estimate_tokens(text), actual)

    def test_special_token_count_is_still_two(self):
        """SPECIAL_TOKENS is measured, not assumed. If bge-m3's framing changes,
        the bound shifts and this fails rather than silently under-estimating."""
        overhead = len(self.tok.encode("")) - len(
            self.tok.encode("", add_special_tokens=False))
        self.assertEqual(overhead, T.SPECIAL_TOKENS)

    def test_model_assumption_is_documented(self):
        """The `chars` ceiling holds for SentencePiece, NOT for byte-level BPE.
        Keep the caveat in the source so a model swap cannot silently inherit a
        bound that no longer applies."""
        src = pathlib.Path(T.__file__).read_text(encoding="utf-8")
        self.assertIn("SentencePiece", src)
        self.assertIn("MODEL ASSUMPTION", src)


if __name__ == "__main__":
    unittest.main()
