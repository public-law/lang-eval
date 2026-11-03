import unittest
from pathlib import Path

from lang_eval import CONFIG, ConfigError, load_config

GOOD = """
[[language]]
name = "Toy"
repos = [{ host = "github", repo = "toy/toy" }]
forum = { kind = "discourse", url = "https://forum.toy.dev" }
zed = ["toy"]
lsp = { name = "toyls", repo = "toy/ls", shared = true }
lsp_rating = 3
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

    def test_reports_every_problem_with_location(self) -> None:
        found = problems("""
[[language]]
name = "Toy"
zedd = ["toy"]
repos = [{ host = "gitlab", repo = "no-slash" }]
forum = { kind = "discourse", url = "http://forum.toy.dev", extra = 1 }
lsp = { repo = "toy/ls" }
lsp_rating = 7
vscode = "toy.toy"

[[language]]
subreddit = 12
""")
        self.assertEqual(sorted(found), sorted([
            "language[0].zedd: unknown key 'zedd'",
            "language[0].repos[0].host: 'gitlab' is not one of codeberg, github",
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

    def test_missing_language_table_and_bad_toml(self) -> None:
        self.assertEqual(problems('title = "x"\n'), ["language: missing required field", "title: unknown key 'title'"])
        self.assertTrue(problems("[[language]\n")[0].startswith("invalid TOML"))


if __name__ == "__main__":
    unittest.main()
