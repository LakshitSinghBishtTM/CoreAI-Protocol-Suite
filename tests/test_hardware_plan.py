from localrt.hardware import Hardware, plan

GB = 1024**3


def test_everything_on_gpu_when_it_fits():
    p = plan(4 * GB, 8192, 32, Hardware("cuda", 32 * GB, 24 * GB))
    assert p == {"ctx": 8192, "n_gpu_layers": 32, "fits": True}


def test_context_shrinks_before_layers():
    p = plan(7 * GB, 32768, 32, Hardware("cuda", 32 * GB, 12 * GB))
    assert p["ctx"] < 8192 and p["n_gpu_layers"] == 32


def test_partial_offload():
    p = plan(14 * GB, 4096, 40, Hardware("cuda", 32 * GB, 8 * GB))
    assert 0 < p["n_gpu_layers"] < 40 and p["fits"]


def test_cpu_and_overrides():
    hw = Hardware("cpu", 16 * GB)
    assert plan(4 * GB, 4096, 32, hw)["n_gpu_layers"] == 0
    p = plan(4 * GB, 4096, 32, hw, ctx_override=1024, ngl_override=5)
    assert p["ctx"] == 1024 and p["n_gpu_layers"] == 5


def test_does_not_fit():
    assert not plan(40 * GB, 4096, 32, Hardware("cpu", 16 * GB))["fits"]
