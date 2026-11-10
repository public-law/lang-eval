import unittest
from pathlib import Path

from lang_eval import CONFIG, ConfigError, load_config, is_prerelease_tag, sparkline, trajectory_direction, ok, missing

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

    def test_missing_language_table_and_bad_toml(self) -> None:
        self.assertEqual(problems('title = "x"\n'), ["language: missing required field", "title: unknown key 'title'"])
        self.assertTrue(problems("[[language]\n")[0].startswith("invalid TOML"))


if __name__ == "__main__":
    unittest.main()
