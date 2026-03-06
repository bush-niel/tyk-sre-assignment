# Tyk SRE Assignment

This project extends the original starter app into a small Kubernetes helper service.

It now supports:
- `/healthz` to confirm the process is up
- `/readyz` to confirm the app can still talk to the Kubernetes API
- `/deployments/health` to compare requested vs ready pods for all Deployments
- `GET /networkpolicies` to list all Calico network policies
- `GET /networkpolicies?managed_only=true` to list Calico network policies created using the tool
- `POST /networkpolicies` to create a bidirectional block between two workloads
- `DELETE /networkpolicies/<name>` to delete one tool-managed policy
- building and pushing a container image with GitHub Actions
- deploying the app with Helm


# Project Structure

```text
python/
├── main.py
├── requirements.txt
├── tests.py
├── Dockerfile
├── README.md
├── app/
│   ├── __init__.py
│   └── app.py
├── docs/
│   ├── examples/README.md
├── helm/
│   └── tyk-sre-tool/
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│           ├── _helpers.tpl
│           ├── clusterrole.yaml
│           ├── clusterrolebinding.yaml
│           ├── deployment.yaml
│           ├── service.yaml
│           └── serviceaccount.yaml
└── .github/
    └── workflows/
        └── docker-build.yml
```

## Why Calico is used here

This project uses Calico `NetworkPolicy` CRDs instead of only native Kubernetes `NetworkPolicy`.

Why:
- native Kubernetes network policy depends on the CNI implementation
- default minikube does not enforce network policies unless a supporting CNI is installed
- Calico supports richer policy behavior such as explicit deny rules, ordering, and more flexible selectors

This assignment needs an on-demand bidirectional deny between workloads selected by namespace and labels, so Calico is a good fit.

## Network policy request format

The API accepts a simple payload based on namespaces and label maps. The app converts the labels into Calico selectors internally.

Example:

```json
{
  "name": "team-a-to-team-b",
  "sourceNamespace": "team-a",
  "sourceLabels": {
    "app": "api-a"
  },
  "targetNamespace": "team-b",
  "targetLabels": {
    "app": "api-b"
  }
}
```

## Managed policies

The tool labels every policy it creates with:
- `managed-by: tyk-sre-tool`
- `policy-group: <request-name>`

That makes it possible to:
- list only tool-managed policies
- avoid deleting policies owned by another team or platform component
