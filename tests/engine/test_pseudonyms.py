"""Phase 14 (audit): pseudonyms are keyed (HMAC), so they cannot be tested against guesses."""

from __future__ import annotations

import hashlib

import pytest

from conversation_agent.app.wiring import configure_pseudonyms_from_env
from conversation_agent.core.models.audit import (
    PseudonymKeyError,
    configure_pseudonym_key,
    log_ref,
    subject_ref,
)
from conversation_agent.core.observability import conversation_ref

PHONE = "5562999990000"
KEY_A = "a" * 40
KEY_B = "b" * 40


def test_the_same_contact_always_gets_the_same_reference_under_one_key() -> None:
    configure_pseudonym_key(KEY_A)
    assert subject_ref("t1", PHONE) == subject_ref("t1", PHONE)
    assert subject_ref("t1", PHONE) != subject_ref("t2", PHONE)  # tenants do not link
    assert PHONE not in subject_ref("t1", PHONE)


def test_a_dictionary_attack_on_the_old_unkeyed_hash_finds_nothing() -> None:
    configure_pseudonym_key(KEY_A)
    reference = subject_ref("t1", PHONE)
    # what anyone holding only the reference and the tenant could compute before the key existed:
    guesses = {
        hashlib.sha256(f"t1\x00{n}".encode()).hexdigest()[:32] for n in (PHONE, "5562999990001")
    }
    assert reference not in guesses


def test_another_key_gives_another_reference_so_a_rotation_is_visible() -> None:
    configure_pseudonym_key(KEY_A)
    first = subject_ref("t1", PHONE)
    configure_pseudonym_key(KEY_B)
    assert subject_ref("t1", PHONE) != first


def test_without_a_key_no_reference_is_produced_instead_of_a_weak_one() -> None:
    configure_pseudonym_key(None)
    with pytest.raises(PseudonymKeyError, match="no pseudonymisation key"):
        subject_ref("t1", PHONE)


def test_a_short_key_is_refused() -> None:
    with pytest.raises(PseudonymKeyError, match="at least 32 bytes"):
        configure_pseudonym_key("too-short")


def test_logging_never_depends_on_the_key_and_never_equals_the_audit_reference() -> None:
    configure_pseudonym_key(None)
    ref = conversation_ref("t1", "conv-1")  # no key: a per-process one, still stable in-process
    assert ref == conversation_ref("t1", "conv-1") and len(ref) == 16
    configure_pseudonym_key(KEY_A)
    assert log_ref("t1", "conv-1") != subject_ref(
        "t1", "conv-1"
    )  # different purpose, different value


def test_the_key_comes_from_the_environment() -> None:
    configure_pseudonym_key(None)
    assert configure_pseudonyms_from_env({}) is False
    assert configure_pseudonyms_from_env({"PSEUDONYM_KEY": KEY_A}) is True
    assert subject_ref("t1", PHONE)
