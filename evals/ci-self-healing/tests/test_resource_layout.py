"""Offline package navigation and review-clause integrity after branch splits."""
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import read_regular
from review_packet import MERGED_SECTIONS, sections
from suite import REPO, skill_from_dir


def prose(text):
    lines, fence = [], None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            marker = match[1][0]
            fence = marker if fence is None else None if fence == marker else fence
        elif fence is None:
            lines.append(line)
    return "\n".join(lines)


def anchors(text):
    found, counts = set(), {}
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*$", prose(text), re.M):
        slug = re.sub(r"[^\w\-\s]", "", heading.lower()).replace(" ", "-")
        number = counts.get(slug, 0)
        found.add(slug + ("-" + str(number) if number else ""))
        counts[slug] = number + 1
    return found


class ResourceLayoutTests(unittest.TestCase):
    def test_runtime_resource_links_and_fragments_resolve_inside_package(self):
        root = (REPO / "skills/ci-self-healing").resolve()
        files = skill_from_dir(root)  # Access guard runs before Markdown bodies.
        for name, raw in files.items():
            if not name.endswith(".md"):
                continue
            for link in re.findall(r"\[[^\]]+\]\(([^)]+)\)", prose(raw.decode())):
                if "://" in link or link.startswith("mailto:"):
                    continue
                path, _, fragment = link.partition("#")
                target = ((root / name).parent / unquote(path)).resolve() if path else root / name
                with self.subTest(source=name, target=link):
                    self.assertIn(root, target.parents)
                    self.assertTrue(target.is_file())
                    if fragment:
                        self.assertIn(unquote(fragment), anchors(read_regular(target).decode()))

    def test_current_review_projection_can_extract_every_declared_clause(self):
        files = skill_from_dir(REPO / "skills/ci-self-healing")
        for name, wanted in MERGED_SECTIONS.items():
            with self.subTest(resource=name):
                self.assertIn(name, files)
                self.assertEqual([c["heading"] for c in sections(files[name].decode(), wanted)], wanted)

    def test_declared_positional_interface_still_has_two_ordered_arguments(self):
        text = read_regular(REPO / "skills/ci-self-healing/SKILL.md").decode()
        frontmatter = text.split("---", 2)[1]
        declaration = re.search(r"^arguments:\s*\[([^]]+)\]$", frontmatter, re.M)
        self.assertIsNotNone(declaration)
        self.assertEqual([part.strip() for part in declaration[1].split(",")], ["work_dir", "output_dir"])


if __name__ == "__main__":
    unittest.main()
