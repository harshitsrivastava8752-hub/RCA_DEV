import os
import sys
import time

from kubernetes import client, config
from kubernetes.client.rest import ApiException

NS = "demo-app"
DEPLOY = "checkout-api"
BAD_URL = "postgresql://app:rotated_password@postgres:5432/orders"
TRAFFIC_POD = "rca-traffic"
TRAFFIC_CODE = (
    "import time, urllib.request\n"
    "while True:\n"
    "    try:\n"
    "        urllib.request.urlopen(urllib.request.Request('http://frontend:8080/checkout', data=b'', method='POST'), timeout=5).read()\n"
    "    except Exception:\n"
    "        pass\n"
    "    time.sleep(0.5)\n"
)


def start_traffic(core):
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(name=TRAFFIC_POD, namespace=NS, labels={"app": TRAFFIC_POD}),
        spec=client.V1PodSpec(
            restart_policy="Always",
            termination_grace_period_seconds=0,
            containers=[
                client.V1Container(
                    name="traffic",
                    image="rca-demo/frontend:v1",
                    image_pull_policy="IfNotPresent",
                    command=["python", "-u", "-c", TRAFFIC_CODE],
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "5m", "memory": "16Mi"}, limits={"memory": "64Mi"}
                    ),
                )
            ],
        ),
    )
    try:
        core.create_namespaced_pod(NS, pod)
    except ApiException as e:
        if e.status != 409:
            raise
    print(f"traffic generator running as pod {TRAFFIC_POD} in {NS}")


def stop_traffic(core):
    try:
        core.delete_namespaced_pod(TRAFFIC_POD, NS)
    except ApiException as e:
        if e.status != 404:
            raise


def wait_rollout(apps, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = apps.read_namespaced_deployment(DEPLOY, NS)
        s = d.status
        want = d.spec.replicas
        if (
            (s.observed_generation or 0) >= d.metadata.generation
            and (s.updated_replicas or 0) == want
            and (s.replicas or 0) == want
            and (s.available_replicas or 0) == want
        ):
            return
        time.sleep(3)
    raise TimeoutError(f"rollout of {DEPLOY} did not finish within {timeout}s")


def apply(core, apps):
    start_traffic(core)
    name = apps.read_namespaced_deployment(DEPLOY, NS).spec.template.spec.containers[0].name
    patch = {
        "spec": {
            "template": {
                "spec": {"containers": [{"name": name, "env": [{"name": "DATABASE_URL", "value": BAD_URL}]}]}
            }
        }
    }
    apps.patch_namespaced_deployment(DEPLOY, NS, patch)
    wait_rollout(apps)
    print("done: checkout-api rolled out with a wrong database password in its pod env")


def revert(core, apps):
    c = apps.read_namespaced_deployment(DEPLOY, NS).spec.template.spec.containers[0]
    if any(e.name == "DATABASE_URL" and e.value == BAD_URL for e in (c.env or [])):
        patch = {
            "spec": {
                "template": {
                    "spec": {"containers": [{"name": c.name, "env": [{"name": "DATABASE_URL", "$patch": "delete"}]}]}
                }
            }
        }
        apps.patch_namespaced_deployment(DEPLOY, NS, patch)
        wait_rollout(apps)
    stop_traffic(core)
    print("reverted: env override removed, traffic generator removed")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("apply", "revert"):
        sys.exit("usage: python scenarios/bad_deployment.py apply|revert")
    config.load_kube_config(context=os.environ.get("KUBE_CONTEXT", "kind-rca-agent"))
    core, apps = client.CoreV1Api(), client.AppsV1Api()
    (apply if sys.argv[1] == "apply" else revert)(core, apps)


if __name__ == "__main__":
    main()
