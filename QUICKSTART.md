# DIAL AWS - Quick Start

## 3 Simple Steps to Install DIAL

### Step 1: Open AWS CloudShell
1. Log into AWS Console
2. Click CloudShell icon (terminal) in top bar
3. Wait for it to load

### Step 2: Download & Configure
```bash
git clone https://github.com/YOUR-ORG/dial-aws-installation.git
cd dial-aws-installation
vi parameters.conf
```

**Fill in:**
- `DOMAIN_NAME` - Your domain (e.g., mycompany.com)
- `DB_PASSWORD` - Strong password (min 8 chars)
- `ADMIN_EMAIL` - Your email

Save: `:`, then `wq!`, then `Enter`

### Step 3: Install
```bash
bash deploy.sh
bash monitor.sh
```

⏱️ Takes ~30 minutes (automated)

---

## After Installation

### 1. Install DIAL App
```bash
helm install dial <CHART> -f helm-values.yaml -n dial --create-namespace
```

### 2. Get Load Balancer DNS
```bash
kubectl get ingress -n dial
```

### 3. Configure DNS
Point these to the Load Balancer DNS:
- chat.yourdomain.com
- admin.yourdomain.com
- core.yourdomain.com
- themes.yourdomain.com

**Don't have Route53?** No problem! See [DNS-CONFIGURATION.md](DNS-CONFIGURATION.md) for detailed instructions with any DNS provider (GoDaddy, Cloudflare, Namecheap, etc.)

### 4. Create Admin User
```bash
bash create-admin-user.sh
```

### 5. Access DIAL
Visit: https://chat.yourdomain.com

---

## Need Help?

See full [README.md](README.md) for detailed instructions and troubleshooting.

## Cost

~$400-650/month on AWS

## Uninstall
```bash
helm uninstall dial -n dial
aws cloudformation delete-stack --stack-name dial-production --region us-east-2
```
