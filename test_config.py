import unittest
from pathlib import Path

from lang_eval import CONFIG, ConfigError, load_config, is_prerelease_tag, sparkline, trajectory_direction, ok, missing, hn_queries, pct_change, series_metric, cached_quarters, recent_quarters, NOW

GOOD = """
[[language]]
name = "Toy"
repos = [{ host = "github", repo = "toy/toy" }]
forum = { kind = "discourse", url = "https://forum.toy.dev" }
zed = ["toy"]
lsp = { name = "toyls", repo = "toy/ls", shared = true }
lsp_rating = 3
hn_query = "Toy language"
packages = { kind = "hex" }
governance = { body = "Toy Board", body_url = "https://toy.dev/board", rfc_url = "https://toy.dev/rfc", sources = ["https://toy.dev/about"] }
"""


def problems(text: str) -> list[str]:
    with unittest.TestCase().assertRaises(ConfigError) as ctx:
        load_config(text)
    return ctx.exception.problems


class ConfigTest(unittest.TestCase):
    def test_current_config_loads(self) -> None:
        self.assertTrue(load_config(Path(CONFIG).read_text()))

    def test_good_config_parses_into_typed_values(self) -> None:
        [lang] = load_config(GOOD)
        self.assertEqual(lang.repos[0].repo, "toy/toy")
        self.assertEqual(lang.zed, ("toy",))
        self.assertIsNotNone(lang.lsp)
        self.assertTrue(lang.lsp and lang.lsp.shared)
        self.assertEqual(lang.hn_query, "Toy language")
        self.assertIsNotNone(lang.packages)
        assert lang.governance is not None
        self.assertEqual(lang.governance.body, "Toy Board")
        self.assertEqual(lang.governance.sources, ("https://toy.dev/about",))

    def test_reports_every_problem_with_location(self) -> None:
        found = problems("""
[[language]]
name = "Toy"
zedd = ["toy"]
repos = [{ host = "bitbucket", repo = "no-slash" }]
forum = { kind = "discourse", url = "http://forum.toy.dev", extra = 1 }
lsp = { repo = "toy/ls" }
lsp_rating = 7
vscode = "toy.toy"

[[language]]
subreddit = 12
""")
        self.assertEqual(sorted(found), sorted([
            "language[0].zedd: unknown key 'zedd'",
            "language[0].repos[0].host: 'bitbucket' is not one of codeberg, github, gitlab",
            "language[0].repos[0].repo: 'no-slash' does not match [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            "language[0].forum.url: expected an https URL with a host, got 'http://forum.toy.dev'",
            "language[0].forum.extra: unknown key 'extra'",
            "language[0].lsp.name: missing required field",
            "language[0].lsp_rating: expected an integer from 1 to 5, got 7",
            "language[0].vscode: expected a list of strings, got 'toy.toy'",
            "language[1].name: missing required field",
            "language[1].subreddit: expected a non-empty string, got 12",
        ]))

    def test_duplicate_names(self) -> None:
        found = problems('[[language]]\nname = "Toy"\n[[language]]\nname = "toy"\n')
        self.assertEqual(found, ["language[1].name: duplicate of language[0] ('toy')"])

    def test_rating_rejects_bool_and_zero(self) -> None:
        self.assertEqual(len(problems('[[language]]\nname = "A"\nlsp_rating = true\n')), 1)
        self.assertEqual(len(problems('[[language]]\nname = "A"\nlsp_rating = 0\n')), 1)

    def test_hn_queries_split_or(self) -> None:
        self.assertEqual(hn_queries('Zig language OR ziglang'), ("Zig language", "ziglang"))
        self.assertEqual(hn_queries('OCaml'), ("OCaml",))
        self.assertEqual(hn_queries('Roc-lang OR "Roc language"'), ("Roc-lang", "Roc language"))

    def test_unknown_governance_and_package_kind(self) -> None:
        found = problems("""
[[language]]
name = "Toy"
packages = { kind = "crates" }
governance = { body = "X", extra = 1, roadmap_updated = "10/31/2026" }
""")
        self.assertTrue(any("packages.kind" in p for p in found))
        self.assertTrue(any("governance.extra" in p for p in found))
        self.assertTrue(any("roadmap_updated" in p for p in found))

    def test_prerelease_tags_and_sparklines(self) -> None:
        self.assertTrue(is_prerelease_tag("ghc-9.12.5-rc4"))
        self.assertTrue(is_prerelease_tag("0.16.0-dev"))
        self.assertTrue(is_prerelease_tag("ghc-10.0-start"))
        self.assertTrue(is_prerelease_tag("wip-branch"))
        self.assertFalse(is_prerelease_tag("ghc-9.12.4"))
        self.assertFalse(is_prerelease_tag("ghc-9.12.4-release"))
        self.assertFalse(is_prerelease_tag("5.5.2"))
        self.assertIn("<svg", sparkline([1, 3, 2, 5]))
        self.assertEqual(sparkline([1]), "")

    def test_trajectory_direction_mixed(self) -> None:
        m = {
            "authors_change_pct": ok(40, None),
            "commits_quarters": ok(-30, None),
            "issue_close_ratio": ok(1.2, None),
        }
        d = trajectory_direction(m)
        self.assertEqual(d["display"], "mixed")
        steady = trajectory_direction({"authors_change_pct": ok(2, None), "commits_quarters": ok(-4, None)})
        self.assertEqual(steady["display"], "flat")
        none = trajectory_direction({"authors_change_pct": missing("x")})
        self.assertIsNone(none["value"])
        no_up_from_missing = trajectory_direction({"authors_change_pct": missing("incomplete"), "commits_quarters": ok(5, None)})
        self.assertEqual(no_up_from_missing["display"], "flat")

    def test_pct_change_requires_measured_prior(self) -> None:
        self.assertIsNone(pct_change(10, None, prior_measured=False))
        self.assertIsNone(pct_change(10, 0, prior_measured=False))
        self.assertEqual(pct_change(10, 0, prior_measured=True), 100.0)
        self.assertIsNone(pct_change(0, 0, prior_measured=True))
        self.assertEqual(pct_change(15, 10, prior_measured=True), 50.0)
        self.assertEqual(pct_change(8, 10, prior_measured=True), -20.0)

    def test_series_metric_skips_unmeasured_prior(self) -> None:
        labels = [q for q, _, _ in recent_quarters()]
        partial = {q: 100 for q in labels[-4:]}
        m = series_metric(partial, "https://example.com/commits")
        self.assertIsNone(m["value"])
        self.assertIn("-", m["display"] or "")
        self.assertIn("unmeasured", m["display"] or "")

    def test_series_metric_measured_zero_prior_is_plus_100(self) -> None:
        labels = [q for q, _, _ in recent_quarters()]
        series = {q: 0 for q in labels[:4]}
        series.update({q: 10 for q in labels[-4:]})
        m = series_metric(series, "https://example.com/commits")
        self.assertEqual(m["value"], 100.0)
        self.assertIn("+100%", m["display"] or "")

    def test_cached_quarters_skips_zeros_and_hn_query_mismatch(self) -> None:
        labels = [q for q, _, end in recent_quarters() if end < NOW]
        self.assertGreaterEqual(len(labels), 2)
        q0, q1 = labels[0], labels[1]
        query = "Zig language OR ziglang"
        stale = {"languages": {"Zig": {
            "hn_quarters": {"query": "old query", "series": {q0: 5, q1: 9}},
            "commits_quarters": {"series": {q0: 0, q1: 12}},
        }}}
        found = cached_quarters("Zig", [stale], query)
        self.assertEqual(found["hn_quarters"], {})
        self.assertEqual(found["commits_quarters"], {q1: 12})
        legacy = {"languages": {"Zig": {"hn_quarters": {"series": {q0: 8, q1: 3}}}}}
        self.assertEqual(cached_quarters("Zig", [legacy], query)["hn_quarters"], {})
        fresh = {"languages": {"Zig": {"hn_quarters": {"query": query, "series": {q0: 8, q1: 0}}}}}
        self.assertEqual(cached_quarters("Zig", [fresh], query)["hn_quarters"], {q0: 8})

    def test_missing_language_table_and_bad_toml(self) -> None:
        self.assertEqual(problems('title = "x"\n'), ["language: missing required field", "title: unknown key 'title'"])
        self.assertTrue(problems("[[language]\n")[0].startswith("invalid TOML"))


if __name__ == "__main__":
    unittest.main()
