from treg import adsconv


def test_usd_to_aud_uses_fixed_rate():
    # 1 AUD = 0.70 USD, so USD converts UP into AUD: US$20.00 -> A$28.57
    assert adsconv.usd_micro_to_aud_micro(20_000_000) == 28_571_428


def test_usd_to_aud_is_integer_only():
    # No float ever appears: 1 micro-USD must not become 1.4285... micro-AUD
    result = adsconv.usd_micro_to_aud_micro(1)
    assert isinstance(result, int)
    assert result == 1


def test_usd_to_aud_zero_and_negative():
    assert adsconv.usd_micro_to_aud_micro(0) == 0
    # Even-divisible negative: -7,000,000 * 10 / 7 = -10,000,000 exactly
    assert adsconv.usd_micro_to_aud_micro(-7_000_000) == -10_000_000
    # Non-exact negative: floor division toward -∞ rounds away from zero
    # -1,000,000 * 10 = -10,000,000; -10,000,000 // 7 = -1,428,572 (not -1,428,571)
    assert adsconv.usd_micro_to_aud_micro(-1_000_000) == -1_428_572


def test_action_ids_cover_every_action():
    assert set(adsconv.CONVERSION_ACTION_IDS) == {
        adsconv.ACTION_SIGNUP, adsconv.ACTION_FIRST_CALL, adsconv.ACTION_PAID
    }
