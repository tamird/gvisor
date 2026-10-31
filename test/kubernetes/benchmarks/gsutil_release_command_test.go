package gsutil

import (
    "encoding/json"
    "testing"

    "gvisor.dev/gvisor/test/kubernetes/testcluster"
)

func TestGSUtilReleaseCommand(t *testing.T) {
    commands := make(map[string][]string)
    for name, flags := range map[string]string{
        "single": `-o "GSUtil:sliced_object_download_threshold=0"`,
        "sliced": "",
    } {
        pod := newGSUtilDevPod(&testcluster.Namespace{Namespace: "qualification"}, "gsutil", "selected", nil, flags)
        commands[name] = pod.Spec.Containers[0].Command
    }
    b, err := json.Marshal(commands)
    if err != nil {
        t.Fatal(err)
    }
    t.Logf("GSUTIL_COMMANDS=%s", b)
}
