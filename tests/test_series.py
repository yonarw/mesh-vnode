from mesh_vnode.series import add_rates, bucket


def local(t, rx, tx=0):
    return {"kind": "local", "rx_time": t, "num_packets_rx": rx, "num_packets_tx": tx}


def test_counters_become_per_hour_rates():
    rows = add_rates([local(0, 100), local(1800, 400)])
    assert "rx_per_hour" not in rows[0]
    assert rows[1]["rx_per_hour"] == 600.0


def test_a_reboot_does_not_produce_a_negative_rate():
    rows = add_rates([local(0, 5000), local(600, 20)])
    assert "rx_per_hour" not in rows[1]


def test_other_kinds_are_left_alone():
    rows = add_rates([{"kind": "gps", "rx_time": 0, "sats_in_view": 9}])
    assert rows == [{"kind": "gps", "rx_time": 0, "sats_in_view": 9}]


def test_bucketing_bounds_the_point_count():
    rows = [{"kind": "gps", "rx_time": t * 60, "sats_in_view": 9, "pdop": 1.4} for t in range(1440)]
    out = bucket(rows, 86400, 100)
    assert len(out) <= 101
    assert all(r["sats_in_view"] == 9 for r in out)


def test_bucketing_averages_measurements_and_keeps_labels():
    rows = [
        {"kind": "gps", "rx_time": 0, "sats_in_view": 8, "precision_bits": 32},
        {"kind": "gps", "rx_time": 10, "sats_in_view": 10, "precision_bits": 13},
        {"kind": "gps", "rx_time": 5000, "sats_in_view": 5, "precision_bits": 32},
    ]
    out = bucket(rows, 10000, 2)
    assert out[0]["sats_in_view"] == 9
    assert out[0]["precision_bits"] == 13  # latest, not an average of 32 and 13


def test_kinds_are_not_blended_in_one_bucket():
    rows = [
        {"kind": "gps", "rx_time": 0, "sats_in_view": 9},
        {"kind": "device", "rx_time": 1, "battery_level": 80},
        {"kind": "gps", "rx_time": 9000, "sats_in_view": 9},
    ]
    out = bucket(rows, 10000, 2)
    assert {r["kind"] for r in out} == {"gps", "device"}
    assert not any("sats_in_view" in r and "battery_level" in r for r in out)


def test_small_series_pass_through_untouched():
    rows = [{"kind": "gps", "rx_time": 0, "sats_in_view": 9}]
    assert bucket(rows, 86400, 360) == rows
