"""Offline package navigation and review-clause integrity after branch splits."""
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import read_regular
from repair_support import EXECUTION_CONTRACT, MODULAR_EXECUTION_CONTRACTS, MERGED_EXECUTION_CONTRACT, execution_contract
from review_packet import CANDIDATE_SECTIONS, sections
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


# Host-side architecture declarations, never runtime instructions. A declared
# explanatory edge still needs semantic review: this does not parse prose into
# authority or prove that a reader obeyed the intended loading order.
EXPLANATORY_EDGES = {
    ("references/" + source + ".md", "references/" + target + ".md")
    for source, target in (
        ("c-cpp-compilation", "diagnosis-and-repair"),
        ("java-maven-compilation", "diagnosis-and-repair"),
        ("dependency-diagnosis-and-repair", "diagnosis-and-repair"),
        ("repair-admission", "diagnosis-and-repair"),
        ("log-evidence-tool", "failure-evidence-extraction"),
        ("result-report", "output-delivery"),
        ("result-report", "pipeline-outcomes"),
        ("remote-verification", "provider-contract"),
        ("workspace-cleanup", "execution-modes"),
        ("workspace-recovery", "execution-modes"),
    )
}
PROVIDER_SELECTION_EDGES = {
    ("references/providers/provider-access.md", "references/providers/github-actions.md"),
}


def reference_graph_issues(files, contract, *, explanatory=EXPLANATORY_EDGES, selections=PROVIDER_SELECTION_EDGES,
                           bound_resources=frozenset(), extension_edges=frozenset()):
    """Validate declared package navigation; do not impose v7 on old packages."""
    if contract not in MODULAR_EXECUTION_CONTRACTS:
        return []
    root = Path("/synthetic-skill")
    edges = set()
    for name, raw in files.items():
        if not name.endswith(".md"):
            continue
        body = prose(raw.decode())
        # Inline and reference-style links are navigation. A backticked file
        # identity is not inferred to be a new loading instruction.
        targets = re.findall(r"\[[^\]]+\]\(([^)]+)\)", body)
        targets += re.findall(r"^\s{0,3}\[[^\]]+\]:\s*(\S+)", body, re.M)
        for link in targets:
            path = unquote(link.partition("#")[0].strip("<>"))
            if not path or "://" in path or path.startswith("mailto:"):
                continue
            target = ((root / name).parent / path).resolve()
            if root not in target.parents:
                continue  # The path containment/link test reports this separately.
            relative = target.relative_to(root).as_posix()
            if relative.startswith("references/") and relative != name:
                edges.add((name, relative))
    references = {name for name in files if name.startswith("references/") and name.endswith(".md")}
    direct = {target for source, target in edges if source == "SKILL.md"}
    selected = {target for source, target in selections if source in direct and (source, target) in edges}
    # Enterprise Profile/rules, policy, case/output bindings are host-declared
    # extensions, not core single-hop routing. Their graph is explicit per fixture;
    # names, directories, or untrusted document links do not create exemptions.
    extensions = set(bound_resources)
    while True:
        reached = {target for source, target in extension_edges if source in extensions and (source, target) in edges}
        if reached <= extensions:
            break
        extensions.update(reached)
    issues = ["not_directly_routed:" + name for name in sorted(references - direct - selected - extensions)]
    for source, target in sorted(selections):
        if source in files and target in files and source not in direct:
            issues.append("selection_source_not_directly_routed:" + source)
    for edge in sorted(edges):
        if edge[0].startswith("references/") and edge not in explanatory | selections | extension_edges:
            issues.append("undeclared_reference_edge:" + "->".join(edge))
    for edge in sorted(explanatory | selections | extension_edges):
        if edge[0] in files and edge[1] in files and edge not in edges:
            issues.append("stale_reference_declaration:" + "->".join(edge))
    return issues


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
        for name, wanted in CANDIDATE_SECTIONS.items():
            with self.subTest(resource=name):
                self.assertIn(name, files)
                self.assertEqual([c["heading"] for c in sections(files[name].decode(), wanted)], wanted)

    def test_current_reference_graph_has_only_declared_indirection(self):
        root = REPO / "skills/ci-self-healing"
        self.assertEqual(reference_graph_issues(skill_from_dir(root), execution_contract(root)), [])

    def test_graph_detects_second_hop_even_when_target_also_has_entry_link(self):
        files = {"SKILL.md": b"[A](references/a.md) [B](references/b.md)",
                 "references/a.md": b"[B](b.md)", "references/b.md": b"leaf"}
        self.assertEqual(reference_graph_issues(files, EXECUTION_CONTRACT, explanatory=set(), selections=set()),
                         ["undeclared_reference_edge:references/a.md->references/b.md"])
        self.assertEqual(reference_graph_issues(files, MERGED_EXECUTION_CONTRACT), [])

    def test_graph_allows_declared_reuse_and_one_selected_provider_but_not_a_chain(self):
        files = {"SKILL.md": b"[A](references/a.md) [B](references/b.md) [P](references/p.md)",
                 "references/a.md": b"[B][prior]\n[prior]: b.md", "references/b.md": b"leaf",
                 "references/p.md": b"[Adapter](adapter.md)", "references/adapter.md": b"leaf"}
        explanatory = {("references/a.md", "references/b.md")}
        selections = {("references/p.md", "references/adapter.md")}
        self.assertEqual(reference_graph_issues(files, EXECUTION_CONTRACT, explanatory=explanatory, selections=selections), [])
        files["references/adapter.md"] = b"[Next](next.md)"
        files["references/next.md"] = b"leaf"
        issues = reference_graph_issues(files, EXECUTION_CONTRACT, explanatory=explanatory, selections=selections)
        self.assertIn("not_directly_routed:references/next.md", issues)
        self.assertIn("undeclared_reference_edge:references/adapter.md->references/next.md", issues)
        chained = selections | {("references/adapter.md", "references/next.md")}
        issues = reference_graph_issues(files, EXECUTION_CONTRACT, explanatory=explanatory, selections=chained)
        self.assertIn("selection_source_not_directly_routed:references/adapter.md", issues)

    def test_trusted_enterprise_extension_does_not_exempt_unrelated_resources(self):
        files = {"SKILL.md": b"[Core](references/core.md)", "references/core.md": b"leaf",
                 "references/company/profile.md": b"[Rules](rules.md)",
                 "references/company/rules.md": b"external rules"}
        options = {"explanatory": set(), "selections": set(),
                   "bound_resources": {"references/company/profile.md"},
                   "extension_edges": {("references/company/profile.md", "references/company/rules.md")}}
        self.assertEqual(reference_graph_issues(files, EXECUTION_CONTRACT, **options), [])
        files["references/company/unbound.md"] = b"unbound"
        self.assertEqual(reference_graph_issues(files, EXECUTION_CONTRACT, **options),
                         ["not_directly_routed:references/company/unbound.md"])

    def test_declared_positional_interface_still_has_two_ordered_arguments(self):
        text = read_regular(REPO / "skills/ci-self-healing/SKILL.md").decode()
        frontmatter = text.split("---", 2)[1]
        declaration = re.search(r"^arguments:\s*\[([^]]+)\]$", frontmatter, re.M)
        self.assertIsNotNone(declaration)
        self.assertEqual([part.strip() for part in declaration[1].split(",")], ["work_dir", "output_dir"])


if __name__ == "__main__":
    unittest.main()
