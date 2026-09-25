"""THROWAWAY — CI-1 step 6: proving the artemis gate fails and blocks a merge.

A gate nobody has seen fail is not a gate. This file fails on purpose. It is
closed without merging.
"""
import unittest


class TestTheGateFails(unittest.TestCase):
    def test_deliberate_failure(self):
        self.assertEqual("this PR", "must not be mergeable")


if __name__ == "__main__":
    unittest.main()
