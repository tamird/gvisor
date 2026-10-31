"""Check actual lost-repair and repeated-feedback packets and congestion state."""

import re

def packet_outcome(report):
    def fields(line):
        return dict(x.split("=", 1) for x in line.split()[1:])
    entries = [(line.split()[0], fields(line)) for line in report["observations"]]
    def selected(kind, mode, phase):
        return [item for event, item in entries if event == kind and item.get("mode") == mode and item.get("phase") == phase]
    def state(mode, phase):
        [record] = selected("FENCE_STATE", mode, phase)
        return {key: int(record[key]) for key in ("mss", "threshold", "rto_us", "ca", "cwnd")}
    errors = []
    def require(condition, error):
        if not condition:
            errors.append(error)
    for mode in ("repeat_ack", "sack_repair"):
        first, last = (state(mode, phase) for phase in ("before_feedback", "after_ack"))
        early = selected("FENCE_WIRE", mode, "after_ack")
        late = selected("FENCE_WIRE", mode, "later_retry")
        expected = (2*first["mss"],first["mss"])
        ranges = [(int(event["offset"]),int(event["bytes"])) for event in early]
        if mode == "repeat_ack":
            require(not ranges, "repeated SACK triggered an early data transmission")
            require(len(late) == 1 and (int(late[0]["offset"]),int(late[0]["bytes"])) == expected, "missing expected retry at next RTO")
            require(state(mode, "later_retry")["rto_us"] == 2*first["rto_us"], "late retry did not double RTO")
        else:
            require(ranges == [expected] and not late, "fresh SACK did not repair exactly the lost MSS before next timeout")
        require(last == first and last["ca"] == 4, "feedback changed timeout-recovery state, congestion budget, or RTO")
    return errors

def traced_replacement(report):
    lines = report["observations"]
    [base] = [int(line.split("base=")[1]) for line in lines if line.startswith("FENCE_FLOW mode=sack_repair ")]
    [mss] = [int(line.split("mss=")[1]) for line in lines if line.startswith("FENCE_STATE mode=sack_repair phase=before_feedback ")]
    seq = (base + 2*mss) % 2**32
    observed = [line for line in report["rack_decisions"] if line.endswith("interrupted=true") and line.startswith("replace ")]
    return observed == [f"replace seq={seq} charged=2 live=1 cwnd=2 interrupted=true"]
