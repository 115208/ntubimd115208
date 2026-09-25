import unittest
from types import SimpleNamespace
from views import baby_utils
from views.edit_family_member import _parse_permissions_from_post


class GrowthAssistantPermissionTestCase(unittest.TestCase):
    def test_feature_keys_includes_growth_assistant(self):
        self.assertIn('growth_assistant', baby_utils.FEATURE_KEYS)

    def test_off_blocks_view_includes_growth_assistant(self):
        self.assertIn('growth_assistant', baby_utils._OFF_BLOCKS_VIEW)

    def test_get_permission_default(self):
        member = SimpleNamespace(permissions={})
        self.assertEqual(baby_utils.get_permission(member, 'growth_assistant'), 'view')

    def test_get_permission_off(self):
        member = SimpleNamespace(permissions={'growth_assistant': 'off'})
        self.assertEqual(baby_utils.get_permission(member, 'growth_assistant'), 'off')
        self.assertFalse(baby_utils.has_permission(member, 'growth_assistant', 'view'))

    def test_get_permission_view(self):
        member = SimpleNamespace(permissions={'growth_assistant': 'view'})
        self.assertEqual(baby_utils.get_permission(member, 'growth_assistant'), 'view')
        self.assertTrue(baby_utils.has_permission(member, 'growth_assistant', 'view'))

    def test_none_member_fails_closed(self):
        self.assertEqual(baby_utils.get_permission(None, 'growth_assistant'), 'off')
        self.assertFalse(baby_utils.has_permission(None, 'growth_assistant', 'view'))

    def test_parse_permissions_from_post_default(self):
        post_data = {}
        perms = _parse_permissions_from_post(post_data)
        self.assertEqual(perms['growth_assistant'], 'view')

    def test_parse_permissions_from_post_off(self):
        post_data = {'perm_growth_assistant': 'off'}
        perms = _parse_permissions_from_post(post_data)
        self.assertEqual(perms['growth_assistant'], 'off')

    def test_parse_permissions_from_post_view(self):
        post_data = {'perm_growth_assistant': 'view'}
        perms = _parse_permissions_from_post(post_data)
        self.assertEqual(perms['growth_assistant'], 'view')


if __name__ == '__main__':
    unittest.main()
