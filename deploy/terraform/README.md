# Terraform example

Installs the Helm chart in `deploy/helm/caips` on an existing cluster.
Unverified: it was written without a cluster or the terraform binary available.

```
kubectl create namespace caips
kubectl -n caips create secret generic caips-provider-keys --from-literal=OPENAI_API_KEY=...
terraform init
terraform plan -var image_repository=registry.example.com/caips -var image_tag=0.1.0
```

SQLite on one volume means one replica. Back up with `python -m coreai.backup create`
(see `docs/deployment.md`) from inside the pod.
