"""Generation-bound DB sessions, with short optimistic commits.

No request-wide transaction. A retired key is useful only to revoke its family;
it never retrieves a new session. Unique cookie names survive response reordering.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
import secrets
import uuid

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore as DBStore
from django.contrib.sessions.models import Session
from django.db import IntegrityError
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from config.db_limits import bounded_atomic

SCOPE_PREFIX = 'exam_scope_'
TOKEN_PREFIX = 'exam_session_'


class SessionConflict(Exception):
    pass


def digest(value):
    return salted_hmac('login-session-revocation', value, algorithm='sha256').hexdigest()


def models():
    from surveys.models import LoginSessionFamily, LoginSessionCredential
    return LoginSessionFamily, LoginSessionCredential


def named_tokens(cookies, prefix):
    result = {}
    for name, value in cookies.items():
        if name.startswith(prefix):
            try:
                identifier = uuid.UUID(hex=name[len(prefix):])
            except ValueError:
                continue
            result[identifier] = value
    if len(result) > settings.SESSION_MAX_GENERATIONS:
        raise SessionConflict('too_many_session_generations')
    return result


@dataclass
class Context:
    family: object = None
    credential: object = None
    secret: str = ''
    scopes: dict = field(default_factory=dict)
    revoke_ids: set = field(default_factory=set)
    legacy: bool = False
    stale: bool = False
    emit: bool = False
    initial_data: object = None
    failure_reason: str = None
    obsolete: set = field(default_factory=set)
    presented_keys: set = field(default_factory=set)


def load_context(cookies):
    Family, Credential = models()
    scopes = named_tokens(cookies, SCOPE_PREFIX)
    tokens = named_tokens(cookies, TOKEN_PREFIX)
    context = Context()
    context.presented_keys = set(tokens.values()) | {
        value for name in (settings.SESSION_COOKIE_NAME, settings.LOGIN_AUTH_COOKIE_NAME)
        if (value := cookies.get(name))
    }
    rows = list(Credential.objects.select_related('family').filter(id__in=tokens).annotate(
        stored_data=Subquery(Session.objects.filter(session_key=OuterRef('session_key')).values('session_data')[:1]),
        stored_expiry=Subquery(Session.objects.filter(session_key=OuterRef('session_key')).values('expire_date')[:1]),
    ).order_by('-created_at'))
    present = {row.id for row in rows}
    context.obsolete = {TOKEN_PREFIX + identifier.hex for identifier in tokens if identifier not in present}
    context.obsolete.update(TOKEN_PREFIX + row.id.hex for row in rows
                            if row.family.active_generation != row.id or row.family.expires_at <= timezone.now())
    known = {row.family.id: row.family for row in rows}
    missing = set(scopes) - set(known)
    if missing:
        known.update((row.id, row) for row in Family.objects.filter(id__in=missing))
    for family in known.values():
        if (family.id in scopes and family.expires_at > timezone.now()
                and constant_time_compare(family.secret_hash, digest(scopes[family.id]))):
            context.scopes[family.id] = (family, scopes[family.id])
    context.revoke_ids = set(context.scopes)
    for credential in rows:
        family = credential.family
        if (family.id in context.scopes and family.active_generation == credential.id
                and family.epoch == credential.epoch
                and constant_time_compare(credential.session_key, tokens[credential.id])):
            context.family, context.credential = family, credential
            context.secret = context.scopes[family.id][1]
            if credential.stored_data is None or credential.stored_expiry <= timezone.now():
                context.stale = True
                context.failure_reason = 'session_record_missing' if credential.stored_data is None else 'session_record_expired'
                return context, None
            context.initial_data = DBStore().decode(credential.stored_data)
            return context, credential.session_key
    # Fixed-name cookies are legacy inputs, never a fallback from scoped tokens.
    legacy_key = cookies.get(settings.LOGIN_AUTH_COOKIE_NAME) or cookies.get(settings.SESSION_COOKIE_NAME)
    if legacy_key:
        previous = Credential.objects.filter(key_hash=digest(legacy_key)).first()
        if previous:
            context.revoke_ids.add(previous.family_id)  # revocation only
            context.stale = True
        elif not scopes and not tokens:
            context.legacy = True
            return context, legacy_key
    if scopes or tokens:
        context.stale = True
        # A revoked/expired family can start a NEW anonymous login. An active
        # family's missing/retired bearer must never be upgraded or overwritten.
        for family, secret in context.scopes.values():
            if family.active_generation is None:
                context.family, context.secret = family, secret
                context.stale = False
                break
    return context, None


_MISSING = object()


def merge(base, desired, current):
    """Three-way merge; concurrent edits of one scalar fail instead of being lost."""
    result = deepcopy(current)
    for key in set(base) | set(desired):
        old, proposed, actual = base.get(key, _MISSING), desired.get(key, _MISSING), current.get(key, _MISSING)
        if old == proposed:
            continue
        if all(isinstance(value, dict) for value in (old, proposed, actual)):
            result[key] = merge(old, proposed, actual)
        elif actual != old and actual != proposed:
            raise SessionConflict('session_write_conflict')
        elif proposed is _MISSING:
            result.pop(key, None)
        else:
            result[key] = deepcopy(proposed)
    return result


class SessionStore(DBStore):
    def __init__(self, session_key=None, *, context=None):
        super().__init__(session_key)
        self.context = context or Context()
        self.original_key = session_key
        self.baseline = None
        self.rotating = False
        self.flushed = False
        self.increments = {}
        self.failure_reason = None
        if self.context.initial_data is not None:
            self._session_cache = deepcopy(self.context.initial_data)
            self.baseline = deepcopy(self.context.initial_data)
        self.failure_reason = self.context.failure_reason

    def load(self):
        old_key = self.session_key
        with bounded_atomic():
            data = super().load()
            if old_key and self.session_key is None:
                self.failure_reason = ('session_record_expired' if Session.objects.filter(
                    session_key=old_key, expire_date__lte=timezone.now()).exists() else 'session_record_missing')
        if self.session_key is None:
            self.original_key = None
        self.baseline = deepcopy(data)
        return data

    def cycle_key(self):
        self._get_session()
        self._session_key = self._get_new_session_key()
        self.rotating = True
        self.modified = True

    def flush(self):
        self._session_cache = {}
        self._session_key = None
        self.flushed = True
        self.rotating = False
        self.modified = True

    def increment(self, key, amount=1):
        self._get_session()
        self.increments[key] = self.increments.get(key, 0) + amount
        self.modified = True

    def save(self, must_create=False, *, finalizing=False):
        Family, Credential = models()
        desired = deepcopy(self._get_session())
        baseline = self.baseline if self.baseline is not None else {}
        ctx = self.context
        if ctx.stale and not self.flushed:
            raise SessionConflict('retired_session')
        with bounded_atomic():
            # Explicit draft saves on a legacy session must not silently promote
            # it before a later 500 response. Adoption belongs to final response.
            if ctx.legacy and ctx.family is None and not finalizing and not self.rotating and not self.flushed and self.original_key:
                row = Session.objects.select_for_update(nowait=True).get(session_key=self.original_key)
                if Credential.objects.filter(key_hash=digest(self.original_key)).exists():
                    raise SessionConflict('retired_legacy_session')
                merged = merge(baseline, desired, row.get_decoded())
                self._session_cache = merged
                Session.objects.filter(session_key=self.original_key).update(
                    session_data=self.encode(merged), expire_date=self.get_expiry_date())
                self._session_cache, self.baseline, self.modified = merged, deepcopy(merged), False
                return
            ids = set(ctx.revoke_ids) if self.flushed else set()
            if self.flushed:
                # Coordinate with adoption/rotation AFTER initial lookup.
                # Historical keys only resolve revocation, never authentication.
                list(Session.objects.select_for_update(nowait=True).filter(
                    session_key__in=ctx.presented_keys).order_by('session_key'))
                ids.update(Credential.objects.filter(key_hash__in=[digest(key) for key in ctx.presented_keys]).values_list('family_id', flat=True))
            if ctx.family is not None:
                ids.add(ctx.family.id)
            locked = {row.id: row for row in Family.objects.select_for_update(nowait=True).filter(id__in=ids).order_by('id')}
            family = locked.get(ctx.family.id) if ctx.family is not None else None
            if self.flushed:
                Session.objects.filter(session_key__in=ctx.presented_keys).delete()
                for row in locked.values():
                    if row.active_generation:
                        ctx.obsolete.add(TOKEN_PREFIX + row.active_generation.hex)
                    keys = Credential.objects.filter(family=row).values_list('session_key', flat=True)
                    Session.objects.filter(session_key__in=keys).delete()
                    row.epoch += 1
                    row.active_generation = None
                    row.save(update_fields=['epoch', 'active_generation'])
                ctx.credential = None
            elif family is not None:
                if (family.epoch != ctx.family.epoch
                        or family.active_generation != (ctx.credential.id if ctx.credential else None)):
                    raise SessionConflict('retired_session')
            if not desired and self.flushed:
                self.flushed = False
                self.modified = False
                return
            if family is None:
                # Atomic one-time adoption of a still-valid legacy key. Never
                # adopt a key already registered as a credential, even retired.
                if self.original_key and Credential.objects.filter(key_hash=digest(self.original_key)).exists():
                    raise SessionConflict('retired_legacy_session')
                ctx.secret = secrets.token_urlsafe(32)
                family = Family.objects.create(secret_hash=digest(ctx.secret),
                                               expires_at=timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE))
            current_key = ctx.credential.session_key if ctx.credential else self.original_key
            current_row = Session.objects.select_for_update(nowait=True).filter(session_key=current_key).first() if current_key else None
            current = current_row.get_decoded() if current_row else {}
            if current_key and current_row is None and not self.flushed:
                raise SessionConflict('session_record_missing')
            if current_row and current_row.expire_date <= timezone.now() and not self.flushed:
                raise SessionConflict('session_record_expired')
            if self.flushed:
                current, baseline = {}, {}
            merged = merge(baseline, desired, current)
            for key, amount in self.increments.items():
                merged[key] = current.get(key, 0) + amount
            self._session_cache = merged
            needs_generation = ctx.credential is None or self.rotating or self.flushed
            if needs_generation:
                if ctx.credential:
                    ctx.obsolete.add(TOKEN_PREFIX + ctx.credential.id.hex)
                key = self.session_key or self._get_new_session_key()
                if self.rotating or self.flushed:
                    family.epoch += 1
                if ctx.legacy and current_key and current_key != key and not self.flushed:
                    # A retiring legacy key can revoke this family, but can
                    # never retrieve its replacement credential or session.
                    Credential.objects.create(family=family, epoch=0,
                                              session_key=current_key, key_hash=digest(current_key))
                # The ordinary session row and generation switch commit together.
                Session.objects.update_or_create(session_key=key, defaults={
                    'session_data': self.encode(merged), 'expire_date': self.get_expiry_date(),
                })
                try:
                    credential = Credential.objects.create(family=family, epoch=family.epoch,
                                                           session_key=key, key_hash=digest(key))
                except IntegrityError as exc:
                    raise SessionConflict('duplicate_session_adoption') from exc
                family.active_generation = credential.id
                if current_key and current_key != key:
                    Session.objects.filter(session_key=current_key).delete()
                ctx.emit = True
            else:
                key, credential = current_key, ctx.credential
                Session.objects.filter(session_key=key).update(session_data=self.encode(merged), expire_date=self.get_expiry_date())
            family.expires_at = max(family.expires_at, self.get_expiry_date())
            family.save(update_fields=['epoch', 'active_generation', 'expires_at'])
            ctx.family, ctx.credential = family, credential
            ctx.scopes[family.id] = (family, ctx.secret)
            ctx.revoke_ids.add(family.id)
        self._session_key = key
        self._session_cache = merged
        self.baseline = deepcopy(merged)
        self.original_key = key
        self.rotating = self.flushed = False
        self.increments.clear()
        self.modified = False
