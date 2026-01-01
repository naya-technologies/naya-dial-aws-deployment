# DIAL Installation Guide for AWS

Welcome! This guide will help you install DIAL on your AWS account in just a few simple steps.

## 📋 What You'll Get

After installation, you'll have:
- **DIAL Chat** - AI assistant interface at `https://chat.yourdomain.com`
- **DIAL Admin** - Management interface at `https://admin.yourdomain.com`
- Complete AWS infrastructure (EKS, RDS, Redis, S3, etc.)
- Secure authentication with AWS Cognito (2 separate user pools)

**Note:** Your domain does NOT need to be in Route53. See [DNS-CONFIGURATION.md](DNS-CONFIGURATION.md) for instructions with any DNS provider.

## ⏱️ Time Required

- **Total time**: 30-40 minutes
- **Active time**: 5 minutes (the rest is automated)

## 💰 Estimated Monthly Cost

- **~$400-650/month** depending on usage
- Main costs: EKS cluster, EC2 nodes, Aurora Serverless, ElastiCache

## 🔑 Prerequisites

Before you begin, make sure you have:

1. ✅ **AWS Account** with admin permissions
2. ✅ **Domain name** (you'll need to configure DNS records)
3. ✅ **Email address** for admin account
5. ✅ **10 minutes** of your time


**That's it!** Everything else is automated.

---

## 🚀 Quick Start (3 Easy Steps)

### Step 1: Open AWS CloudShell

1. Log in to your AWS Console
2. Make sure you're in your preferred region (e.g., us-east-2)
3. Click the CloudShell icon (terminal icon) in the top navigation bar
4. Wait for CloudShell to load

### Step 2: Download Installation Files

In CloudShell, run these commands:

```bash
# Download the installation package
git clone https://github.com/naya-technologies/naya-dial-aws-deployment.git
cd dial-aws-installation
```

### Step 3: Configure and Install

```bash
# Edit the configuration file
vi parameters.conf
```

**Fill in these required values:**
- `DOMAIN_NAME` - Your domain (e.g., `mycompany.com`)
- `DB_PASSWORD` - A strong password (min 8 chars, use letters, numbers, symbols)
- `ADMIN_EMAIL` - Your email address

**Save the file:**
- Press `:`
- Press `wq!` to confirm
- Press `Enter`

**Generate ACM record**
```bash
# Generate Certificate Request
bash create-certificate.sh
```

Add DNS records to approve request in DNS  

```bash
# Validate certificates approved
bash verify-certificate.sh
```  

**Run the installation:**

```bash
bash deploy.sh
bash monitor.sh
```

That's it! The script will:
- ✅ Create all AWS resources (VPC, EKS, RDS, etc.)
- ✅ Configure security groups and networking
- ✅ Set up authentication
- ✅ Generate configuration files

**This takes about 30 minutes** - grab a coffee! ☕

**Deploy DIAL platoform**
```bash
# Deploy components and LBs of DIAL
bash post-deploy.sh
```

==========================================
✅ DIAL Deployment Complete!"
==========================================

Files created:
  📄 deployment-outputs.env  - Infrastructure outputs
  📄 helm-values.yaml        - Helm values used

⚠️  FINAL STEPS:
  1. Add the 3 DNS records shown above
  2. Wait 5-30 minutes for DNS propagation
  3. Access DIAL:
     - Chat:  https://chat.${DOMAIN_NAME}"
     - Admin: https://admin.${DOMAIN_NAME}"

🔍 Verify deployment:
  kubectl get pods -n dial
  kubectl get ingress -n dial

⚠️  SECURITY: Keep deployment-outputs.env and helm-values.yaml secure!
   They contain passwords and secrets.

---

## 📝 Detailed Instructions

### What the Script Does

The installation scripts automatically:

1. **Validates** your configuration
2. **Creates** an S3 bucket for templates
3. **Deploys** CloudFormation stacks:
   - VPC with public/private subnets
   - EKS Kubernetes cluster
   - RDS PostgreSQL database
   - ElastiCache Redis
   - S3 storage bucket
   - Cognito user authentication
   - Security groups and IAM roles
4. **Configures** kubectl for EKS
5. **Installs** AWS Load Balancer Controller
6. **Generates** Helm configuration with all credentials

### Configuration File Reference

Open `parameters.conf` and fill in the following:

#### Required Parameters (YOU MUST FILL THESE IN)

```bash
# Your domain name - DIAL will use subdomains of this
# Example: if you put "mycompany.com", DIAL will be at chat.mycompany.com
DOMAIN_NAME="mycompany.com"

# Database password - make it strong!
# Must be at least 8 characters with uppercase, lowercase, numbers, and symbols
DB_PASSWORD="MySecurePassword123!"

# Your email - this will be the first admin user
ADMIN_EMAIL="admin@mycompany.com"
```

#### Optional Parameters (Can Leave as Default)

```bash
# AWS Region - where everything will be created
AWS_REGION="il-central-1"  # Tel Aviv

# Name for your installation
STACK_NAME="dial-production"

# EKS cluster name
EKS_CLUSTER_NAME="dial-cluster"

# Cognito User Pool names
COGNITO_USER_POOL_NAME="dial-users"        # For chat users
COGNITO_ADMIN_USER_POOL_NAME="dial-admins" # For administrators

# SSL Certificate option
# "auto" = we create one for you (requires DNS validation)
# "existing" = you provide an existing ACM certificate ARN
CERTIFICATE_OPTION="auto"

# If using existing certificate:
EXISTING_CERTIFICATE_ARN=""

# Allow users to create their own accounts?
# "no" (recommended) = only admins can create users
# "yes" = anyone can sign up (applies to chat users only, not admins)
ALLOW_SELF_REGISTRATION="no"
```

---

## 🎯 After Installation

After the `deploy.sh` script completes, you need to do a few manual steps:

### 1. Install DIAL Application

The infrastructure is ready, but you need to install the DIAL application:

```bash
# You need to get the DIAL Helm chart from your DIAL provider
# Then run:
helm install dial <PATH_TO_DIAL_CHART> -f helm-values.yaml -n dial --create-namespace
```

### 2. Configure DNS Records

Wait for the Load Balancer to be created:

```bash
kubectl get ingress -n dial -w
```

When you see an ADDRESS column with a DNS name like `k8s-xxx.us-east-2.elb.amazonaws.com`, that's your Load Balancer.

Get the exact DNS name:

```bash
kubectl get ingress -n dial -o jsonpath='{.items[0].status.loadBalancer.ingress[0].hostname}'
```

**Now go to your DNS provider** (GoDaddy, Cloudflare, Route53, etc.) and create these CNAME records:

| Type  | Name                    | Value                          |
|-------|-------------------------|--------------------------------|
| CNAME | chat.mycompany.com      | k8s-xxx.us-east-2.elb.amazonaws.com |
| CNAME | admin.mycompany.com     | k8s-xxx.us-east-2.elb.amazonaws.com |
| CNAME | core.mycompany.com      | k8s-xxx.us-east-2.elb.amazonaws.com |
| CNAME | themes.mycompany.com    | k8s-xxx.us-east-2.elb.amazonaws.com |

**Note**: DNS changes can take 5-60 minutes to propagate.

### 5. Create Users

DIAL uses **TWO separate Cognito User Pools** for enhanced security:

#### Admin User Pool (for Admin Portal)
For administrators who manage the system:

```bash
bash create-admin-user.sh
```

**Features:**
- Access to Admin portal (https://admin.yourdomain.com)
- MFA **required** for security
- 12-character minimum password
- Admin-only user creation
- 30-minute token expiry

#### Chat User Pool (for End Users)
For regular users who use the Chat interface:

```bash
bash create-chat-user.sh
```

**Features:**
- Access to Chat interface (https://chat.yourdomain.com)
- MFA optional
- 8-character minimum password
- Self-registration (optional, configurable)
- 60-minute token expiry

**Why two pools?**
- **Security**: Admins have stricter requirements (MFA, stronger passwords)
- **Isolation**: Admin accounts are completely separate from user accounts
- **Flexibility**: Different policies for different user types

**Summary:**
- Created an admin? → Use `create-admin-user.sh` → Access https://admin.yourdomain.com
- Created a chat user? → Use `create-chat-user.sh` → Access https://chat.yourdomain.com

### 4. Access DIAL

Once DNS has propagated, visit:
- **Chat**: `https://chat.yourdomain.com`
- **Admin**: `https://admin.yourdomain.com`

Log in with your email and the temporary password sent to you.

---

## 🔧 Troubleshooting

### Problem: "Stack already exists" error

**Solution**: You already have a stack with this name. Either:
- Choose a different `STACK_NAME` in `parameters.conf`, or
- Delete the existing stack first

### Problem: kubectl can't connect to cluster

**Solution**: Update your kubeconfig:
```bash
aws eks update-kubeconfig --name dial-cluster --region us-east-2
```

### Problem: Can't access DIAL after DNS configuration

**Checklist**:
1. ✅ DNS records point to the correct Load Balancer DNS?
2. ✅ Waited at least 10 minutes for DNS to propagate?
3. ✅ HTTPS certificate validated (if using auto mode)?
4. ✅ Helm installation completed successfully?

Check certificate status:
```bash
aws acm describe-certificate --certificate-arn <CERT_ARN> --region us-east-2
```

### Problem: Pods not starting

Check pod status:
```bash
kubectl get pods -n dial
kubectl describe pod <POD_NAME> -n dial
kubectl logs <POD_NAME> -n dial
```

### Problem: Out of memory or CPU

Scale up your node group:
```bash
# Edit parameters.conf and increase EKS_NODE_DESIRED_SIZE
# Then update the stack:
bash install.sh
# Answer "yes" when asked about updating existing stack
```

---

## 🗑️ Uninstalling DIAL

If you need to remove DIAL completely:

```bash
# 1. Delete Helm release
helm uninstall dial -n dial

# 2. Wait for Load Balancers to be deleted
sleep 60

# 3. Delete CloudFormation stack
aws cloudformation delete-stack --stack-name dial-production --region us-east-2

# 4. Wait for deletion to complete
aws cloudformation wait stack-delete-complete --stack-name dial-production --region us-east-2

# 5. Delete S3 buckets (CloudFormation can't delete non-empty buckets)
aws s3 rb s3://dial-production-cfn-templates-<ACCOUNT_ID> --force
aws s3 rb s3://dial-storage-<ACCOUNT_ID>-us-east-2 --force
```

**Warning**: This will permanently delete all data!

---

## 📂 Files in This Package

- `install.sh` - Main installation script (run this!)
- `parameters.conf` - Configuration file (edit this!)
- `cloudformation/` - CloudFormation templates (don't edit)
  - `dial-main.yaml` - Main orchestration template
  - `dial-vpc.yaml` - Network infrastructure
  - `dial-eks.yaml` - Kubernetes cluster
  - `dial-iam.yaml` - IAM roles and permissions
  - `dial-rds.yaml` - PostgreSQL database
  - `dial-cache.yaml` - Redis cache
  - `dial-storage.yaml` - S3 storage
  - `dial-cognito.yaml` - User authentication
- `next-steps.sh` - Post-installation instructions
- `create-admin-user.sh` - Script to create admin user

**After installation, these files will be created:**
- `helm-values.yaml` - Helm configuration (⚠️ KEEP SECRET)
- `installation-info.txt` - Installation summary

---

## 🔒 Security Best Practices

1. **Keep `helm-values.yaml` secure** - It contains passwords and API keys
2. **Use a strong database password** - At least 12 characters
3. **Enable MFA** in Cognito for production use
4. **Regularly update** EKS and node AMIs
5. **Monitor costs** using AWS Cost Explorer
6. **Enable CloudTrail** for audit logging
7. **Review security groups** regularly

---

## 📞 Getting Help

### Check Installation Logs

All installation steps are logged. If something fails, check:

```bash
# CloudFormation events
aws cloudformation describe-stack-events --stack-name dial-production --region us-east-2

# Kubernetes logs
kubectl logs -n dial -l app=dial-core
```

### Common Questions

**Q: How much will this cost?**
A: Approximately $300-500/month depending on usage. Main costs are EKS ($73/mo), EC2 nodes (~$150/mo), and RDS (~$60/mo).

**Q: Can I use my existing VPC?**
A: The provided templates create a new VPC. For existing VPC integration, you'll need to modify the CloudFormation templates.

**Q: Can I change the region after installation?**
A: No, you'll need to install in a new region and migrate data.

**Q: How do I back up my data?**
A: RDS automated backups are enabled (7 days retention). For S3, enable versioning and cross-region replication.

**Q: Can I scale up/down?**
A: Yes, edit `parameters.conf` and update the stack by running `install.sh` again.

---

## 📄 License

[Add your license information]

## 🙏 Support

For technical support, contact: [your-support-email]

For DIAL product questions: [DIAL support contact]

---

**That's it! Enjoy using DIAL! 🎉**
