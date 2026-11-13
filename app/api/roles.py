SUPER_ADMIN_EMAIL = "abdullahtauseef988@gmail.com"


def is_super_admin(user) -> bool:
    return user.email.strip().lower() == SUPER_ADMIN_EMAIL


# These take anything with a `.role`: a workspace Member for tenant permissions.
def is_admin(user) -> bool:
    return user.role in {"admin", "super_admin"}


def is_reviewer(user) -> bool:
    return user.role in {"manager", "admin", "super_admin"}


def is_platform_admin(user) -> bool:
    """Operator of the whole installation (users.role), not of one workspace."""
    return is_super_admin(user) or user.role in {"admin", "super_admin"}
