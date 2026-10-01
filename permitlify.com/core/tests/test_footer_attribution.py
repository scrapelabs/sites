from pathlib import Path
import unittest


ATTRIBUTION = "Made by Khemiri Mohamed"
REPO_ROOT = Path(__file__).resolve().parents[3]


FOOTER_TEMPLATES = {
    "Permitlify public pages": REPO_ROOT
    / "permitlify.com"
    / "templates"
    / "core"
    / "public_base.html",
    "Permitlify homepage": REPO_ROOT
    / "permitlify.com"
    / "templates"
    / "core"
    / "index.html",
    "Permitlify app pages": REPO_ROOT
    / "permitlify.com"
    / "templates"
    / "core"
    / "base.html",
    "GoldenProxies": REPO_ROOT
    / "goldenproxies.com"
    / "artifacts"
    / "goldenproxies-django"
    / "core"
    / "templates"
    / "base.html",
}


class FooterAttributionTests(unittest.TestCase):
    def test_public_footer_copyright_lines_credit_khemiri_mohamed(self):
        for site_name, template_path in FOOTER_TEMPLATES.items():
            with self.subTest(site=site_name):
                template = template_path.read_text(encoding="utf-8")
                attribution_line = next(
                    (line for line in template.splitlines() if ATTRIBUTION in line),
                    "",
                )

                self.assertIn(ATTRIBUTION, attribution_line)
                self.assertTrue(
                    "&copy;" in attribution_line or "©" in attribution_line,
                    f"{site_name} attribution should sit next to the copyright text.",
                )


if __name__ == "__main__":
    unittest.main()
