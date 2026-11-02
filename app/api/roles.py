SUPER_ADMIN_EMAIL = "abdullahtauseef988@gmail.com"


def is_super_admin(user) -> bool:
    return user.email.strip().lower() == SUPER_ADMIN_EMAIL


def is_admin(user) -> bool:
    return user.role in {"admin", "super_admin"}


def is_reviewer(user) -> bool:
    return user.role in {"manager", "admin", "super_admin"}
