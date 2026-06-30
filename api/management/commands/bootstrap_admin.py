"""Create or update an initial admin account.

The self-hosted (local) deployment has no Firebase to mint the first account,
and every account-creating API path presupposes an already-authenticated
CongUser. This command bootstraps that first account so the app is usable:

  * a Django auth user (username = email, so authenticate() works at login),
  * a Congregation,
  * an admin CongUser linking the two.

It is idempotent — safe to run on every deploy. The password is taken from
``--password`` or, preferably, the ``ORGANIZED_ADMIN_PASSWORD`` environment
variable (so it never appears in the process arguments / ps output).

Example::

    ORGANIZED_ADMIN_PASSWORD='secret' python manage.py bootstrap_admin \
        --email admin@example.com --cong-name 'My Congregation' --admin
"""

import os
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.models import Congregation, CongUser


class Command(BaseCommand):
    help = "Create/update the initial admin (Django user + congregation + admin CongUser). Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument(
            "--password",
            default=None,
            help="Admin password. Prefer the ORGANIZED_ADMIN_PASSWORD env var to keep it out of ps.",
        )
        parser.add_argument("--cong-name", dest="cong_name", required=True)
        parser.add_argument("--cong-number", dest="cong_number", default="")
        parser.add_argument("--country-code", dest="country_code", default="")
        parser.add_argument("--firstname", default="Admin")
        parser.add_argument("--lastname", default="")
        parser.add_argument(
            "--admin",
            action="store_true",
            help="Grant the 'admin' congregation role (recommended for the first account).",
        )
        parser.add_argument(
            "--superuser",
            action="store_true",
            help=(
                "Also grant Django is_superuser + is_staff (Django admin access). "
                "Separate from the 'admin' congregation role. Grant-only: this never "
                "revokes the flags, so dropping it later does not demote the account."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        email = (opts["email"] or "").strip()
        password = opts["password"] or os.environ.get("ORGANIZED_ADMIN_PASSWORD")
        if not email:
            raise CommandError("--email is required")
        if not password:
            raise CommandError(
                "password required via --password or the ORGANIZED_ADMIN_PASSWORD env var"
            )

        User = get_user_model()

        # 1. Django auth user — username == email so the local LoginView's
        #    authenticate(username=email, password=...) resolves it.
        user, user_created = User.objects.get_or_create(
            username=email, defaults={"email": email}
        )
        user.email = email
        user.set_password(password)  # keep the password in sync with config
        if opts["superuser"]:
            # Grant-only: enable Django admin access, but never revoke it here so
            # a later run without the flag can't silently lock out the operator.
            user.is_superuser = True
            user.is_staff = True
        user.save()

        # 2. Congregation (keyed on name for idempotency).
        cong, cong_created = Congregation.objects.get_or_create(
            cong_name=opts["cong_name"],
            defaults={
                "cong_id": str(uuid.uuid4()),
                "cong_number": opts["cong_number"],
                "country_code": opts["country_code"],
            },
        )

        # 3. admin CongUser linking the auth user to the congregation.
        cong_user, profile_created = CongUser.objects.get_or_create(
            auth_user=user,
            defaults={
                "congregation": cong,
                "firstname": opts["firstname"],
                "lastname": opts["lastname"],
                "cong_role": ["admin"] if opts["admin"] else [],
            },
        )

        changed = False
        if cong_user.congregation_id != cong.id:
            cong_user.congregation = cong
            changed = True
        if opts["admin"]:
            roles = list(cong_user.cong_role or [])
            if "admin" not in roles:
                roles.append("admin")
                cong_user.cong_role = roles
                changed = True
        if changed:
            cong_user.save()

        self.stdout.write(
            self.style.SUCCESS(
                "bootstrap_admin: "
                f"user={'created' if user_created else 'updated'}, "
                f"congregation={'created' if cong_created else 'exists'}, "
                f"cong_user={'created' if profile_created else 'exists'} "
                f"(email={email}, cong='{cong.cong_name}', "
                f"roles={cong_user.cong_role}, superuser={user.is_superuser})"
            )
        )
