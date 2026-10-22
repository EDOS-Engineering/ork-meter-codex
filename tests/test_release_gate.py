"""Missing or weakened production protections refuse release publication."""
import copy
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / ".github/scripts/check-release-gate.py"
spec = importlib.util.spec_from_file_location("release_gate", path)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class ReleaseGate(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "name": "production", "can_admins_bypass": False,
            "protection_rules": [{"type": "required_reviewers", "prevent_self_review": False,
                                  "reviewers": [{"type": "User", "reviewer": {"id": 1}}]}],
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        self.branches = {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]}

    def test_protected_environment_is_accepted(self):
        gate.validate(self.environment, self.branches)

    def test_missing_or_weakened_review_is_rejected(self):
        variants = [{}, dict(self.environment, protection_rules=[]),
                    dict(self.environment, can_admins_bypass=True)]
        for field, value in (("reviewers", []), ("prevent_self_review", True), ("prevent_self_review", None)):
            env = copy.deepcopy(self.environment)
            env["protection_rules"][0][field] = value
            variants.append(env)
        for env in variants:
            with self.subTest(environment=env), self.assertRaises(ValueError):
                gate.validate(env, self.branches)

    def test_unrestricted_or_extra_branch_and_tag_policies_are_rejected(self):
        for policy in (None, {}, {"protected_branches": True, "custom_branch_policies": False}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                gate.validate(dict(self.environment, deployment_branch_policy=policy), self.branches)
        for policies in ([], [{"name": "*", "type": "branch"}], [{"name": "main", "type": "tag"}],
                         self.branches["branch_policies"] + [{"name": "dev", "type": "branch"}]):
            with self.subTest(policies=policies), self.assertRaises(ValueError):
                gate.validate(self.environment, {"total_count": len(policies), "branch_policies": policies})


if __name__ == "__main__":
    unittest.main()
