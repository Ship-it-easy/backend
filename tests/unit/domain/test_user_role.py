from auth.domain.user_role import UserRoleEnum


class TestUserRoleEnum:

    def test_admin_value(self):
        assert UserRoleEnum.ADMIN == "admin"

    def test_user_value(self):
        assert UserRoleEnum.USER == "user"

    def test_role_is_str_enum(self):
        assert isinstance(UserRoleEnum.ADMIN, str)
        assert isinstance(UserRoleEnum.USER, str)
