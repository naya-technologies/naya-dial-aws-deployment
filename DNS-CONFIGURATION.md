# DNS Configuration Guide - For Domains NOT in Route53

## Overview

Your domain does NOT need to be in Route53 to use DIAL. You can use any DNS provider (GoDaddy, Cloudflare, Namecheap, etc.).

---

## 📋 What You Need to Know

### DNS Records You'll Create

After DIAL installation completes, you'll need to create these DNS records:

| Type | Name | Points To |
|------|------|-----------|
| CNAME | chat.yourdomain.com | ALB DNS (from AWS) |
| CNAME | admin.yourdomain.com | ALB DNS (from AWS) |
| CNAME | core.yourdomain.com | ALB DNS (from AWS) |
| CNAME | themes.yourdomain.com | ALB DNS (from AWS) |

**Plus one more for SSL certificate validation** (if using auto-created certificate)

---

## 🔐 SSL Certificate Options

### Option 1: Auto-Create Certificate (ACM) - Requires DNS Validation

**In parameters.conf:**
```bash
CERTIFICATE_OPTION="auto"
```

**What happens:**
1. AWS creates a free SSL certificate
2. AWS asks you to prove you own the domain
3. You add a special DNS record for validation
4. After validation, certificate is issued

**Steps:**

1. **Run installation**
   ```bash
   bash deploy.sh
   bash monitor.sh
   ```

2. **Get validation DNS record**
   ```bash
   # Get certificate ARN from stack outputs
   CERT_ARN=$(aws cloudformation describe-stacks \
     --stack-name dial-production \
     --query "Stacks[0].Outputs[?OutputKey=='CertificateArn'].OutputValue" \
     --output text)
   
   # Get validation CNAME record
   aws acm describe-certificate \
     --certificate-arn $CERT_ARN \
     --query 'Certificate.DomainValidationOptions[0]'
   ```

3. **You'll see something like:**
   ```json
   {
     "DomainName": "*.yourdomain.com",
     "ValidationDomain": "yourdomain.com",
     "ValidationStatus": "PENDING_VALIDATION",
     "ResourceRecord": {
       "Name": "_abc123.yourdomain.com",
       "Type": "CNAME",
       "Value": "_xyz789.acm-validations.aws."
     }
   }
   ```

4. **Add this DNS record in your DNS provider:**
   - Type: `CNAME`
   - Name: `_abc123.yourdomain.com` (copy from output)
   - Value: `_xyz789.acm-validations.aws.` (copy from output)
   - TTL: `300` (5 minutes)

5. **Wait for validation** (5-30 minutes)
   ```bash
   # Check status
   aws acm describe-certificate \
     --certificate-arn $CERT_ARN \
     --query 'Certificate.Status'
   ```

6. **When status is "ISSUED"**, continue to next steps

---

### Option 2: Use Existing Certificate

If you already have an SSL certificate in AWS Certificate Manager:

**In parameters.conf:**
```bash
CERTIFICATE_OPTION="existing"
EXISTING_CERTIFICATE_ARN="arn:aws:acm:il-central-1:123456789012:certificate/your-cert-id"
```

**Requirements:**
- Certificate must be in **same region** as your DIAL deployment
- Certificate must cover:
  - `*.yourdomain.com` (wildcard), OR
  - Individual domains: `chat.yourdomain.com`, `admin.yourdomain.com`, etc.

---

### Option 3: Import Your Own Certificate

If you have a certificate from another provider (Let's Encrypt, etc.):

1. **Import to ACM:**
   ```bash
   aws acm import-certificate \
     --certificate fileb://certificate.pem \
     --private-key fileb://private-key.pem \
     --certificate-chain fileb://certificate-chain.pem \
     --region il-central-1
   ```

2. **Get the ARN:**
   ```bash
   aws acm list-certificates --region il-central-1
   ```

3. **Use in parameters.conf:**
   ```bash
   CERTIFICATE_OPTION="existing"
   EXISTING_CERTIFICATE_ARN="arn:aws:acm:il-central-1:123456789012:certificate/your-cert-id"
   ```

---

## 🌐 DNS Provider Specific Instructions

### GoDaddy

1. Log in to GoDaddy
2. Go to **My Products** → **Domains**
3. Click **DNS** next to your domain
4. Click **Add** for each record
5. For CNAME records:
   - Type: `CNAME`
   - Name: `chat` (not chat.yourdomain.com)
   - Value: `k8s-dial-xxx.il-central-1.elb.amazonaws.com`
   - TTL: `600`

### Cloudflare

1. Log in to Cloudflare
2. Select your domain
3. Go to **DNS** → **Records**
4. Click **Add record**
5. For CNAME records:
   - Type: `CNAME`
   - Name: `chat`
   - Target: `k8s-dial-xxx.il-central-1.elb.amazonaws.com`
   - Proxy status: **DNS only** (gray cloud, not orange)
   - TTL: `Auto`

**IMPORTANT for Cloudflare:**
- Set proxy to **DNS only** (gray cloud)
- If you use orange cloud (proxied), SSL might have issues

### Namecheap

1. Log in to Namecheap
2. Go to **Domain List**
3. Click **Manage** next to your domain
4. Go to **Advanced DNS** tab
5. Click **Add New Record**
6. For CNAME records:
   - Type: `CNAME Record`
   - Host: `chat`
   - Value: `k8s-dial-xxx.il-central-1.elb.amazonaws.com`
   - TTL: `Automatic`

### Google Domains / Google Cloud DNS

1. Log in to Google Domains or Google Cloud Console
2. Select your domain
3. Go to **DNS** settings
4. Click **Manage custom records**
5. For CNAME records:
   - Type: `CNAME`
   - Host name: `chat`
   - Data: `k8s-dial-xxx.il-central-1.elb.amazonaws.com`
   - TTL: `3600`

### Generic DNS Provider

If your provider isn't listed:

1. Find the **DNS Management** or **DNS Settings** page
2. Look for **Add Record** or **Manage Records**
3. Add CNAME records with these values:
   - Type/Record Type: `CNAME`
   - Name/Host/Subdomain: `chat`, `admin`, `core`, `themes`
   - Value/Target/Points to: `<ALB_DNS_FROM_AWS>`
   - TTL: `300` to `3600` (anything is fine)

---

## 📝 Complete Installation Flow with DNS

### Step-by-Step Process

#### 1. Before Installation

**In parameters.conf:**
```bash
DOMAIN_NAME="yourdomain.com"
CERTIFICATE_OPTION="auto"  # or "existing"
```

#### 2. Run Installation

```bash
bash install.sh
# Wait ~30 minutes
```

#### 3. Get Load Balancer DNS

```bash
# After installation completes
kubectl get ingress -n dial

# Or get it from CloudFormation
ALB_DNS=$(kubectl get ingress -n dial dial-chat-ingress \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

echo "Your Load Balancer DNS: $ALB_DNS"
```

**Example output:**
```
k8s-dial-dialchat-abc123def456-1234567890.il-central-1.elb.amazonaws.com
```

#### 4. If Using Auto-Certificate: Validate Domain

```bash
# Get certificate ARN
CERT_ARN=$(aws cloudformation describe-stacks \
  --stack-name dial-production \
  --query "Stacks[0].Outputs[?OutputKey=='CertificateArn'].OutputValue" \
  --output text)

# Get validation record
aws acm describe-certificate \
  --certificate-arn $CERT_ARN \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord'
```

**Add this DNS record in your DNS provider** (see provider-specific instructions above)

Wait for certificate validation:
```bash
# Check every few minutes
aws acm describe-certificate \
  --certificate-arn $CERT_ARN \
  --query 'Certificate.Status'

# Wait until output is: "ISSUED"
```

#### 5. Add Application DNS Records

In your DNS provider, add these CNAME records:

| Name | Value |
|------|-------|
| chat.yourdomain.com | `<ALB_DNS>` |
| admin.yourdomain.com | `<ALB_DNS>` |
| core.yourdomain.com | `<ALB_DNS>` |
| themes.yourdomain.com | `<ALB_DNS>` |

**Replace `<ALB_DNS>`** with the actual value from step 3.

#### 6. Wait for DNS Propagation

DNS changes can take 5-60 minutes to propagate worldwide.

**Check DNS propagation:**
```bash
# Test DNS resolution
nslookup chat.yourdomain.com

# Or use online tools:
# https://www.whatsmydns.net/
```

#### 7. Test Access

Once DNS is propagated and certificate is validated:

```bash
# Test chat
curl -I https://chat.yourdomain.com

# Should see: HTTP/2 200
```

Visit in browser:
- https://chat.yourdomain.com
- https://admin.yourdomain.com

---

## 🔍 Troubleshooting

### Problem: Certificate stays in PENDING_VALIDATION

**Check:**
1. Did you add the validation CNAME record?
2. Is the record exactly as provided (including the trailing dot)?
3. Did you wait 10-15 minutes?

**Debug:**
```bash
# Check if DNS record is visible
nslookup -type=CNAME _abc123.yourdomain.com

# Should return the validation value
```

### Problem: DNS not resolving

**Check:**
1. Did you use the correct Load Balancer DNS?
2. Did you create all 4 CNAME records?
3. Did you wait for propagation (5-60 minutes)?

**Debug:**
```bash
# Check what your DNS returns
dig chat.yourdomain.com

# Check worldwide propagation
# Visit: https://www.whatsmydns.net/
```

### Problem: SSL Certificate Error in Browser

**Possible causes:**
1. Certificate not validated yet
2. DNS pointing to wrong place
3. Cloudflare proxy enabled (orange cloud)

**Solutions:**
1. Wait for certificate validation to complete
2. Verify ALB DNS is correct
3. If using Cloudflare, set to "DNS only" (gray cloud)

### Problem: 502 Bad Gateway

**Causes:**
1. Pods not running yet
2. Health checks failing

**Debug:**
```bash
# Check pods
kubectl get pods -n dial

# Check ingress
kubectl describe ingress -n dial

# Check ALB target health
aws elbv2 describe-target-health \
  --target-group-arn <TARGET_GROUP_ARN>
```

---

## 📋 Quick Reference

### Get All Important Values

```bash
# Stack name
STACK_NAME="dial-production"
AWS_REGION="il-central-1"

# Get Load Balancer DNS
kubectl get ingress -n dial -o wide

# Get Certificate ARN
aws cloudformation describe-stacks \
  --stack-name $STACK_NAME \
  --query "Stacks[0].Outputs[?OutputKey=='CertificateArn'].OutputValue" \
  --output text

# Get Certificate Status
CERT_ARN="<from-above>"
aws acm describe-certificate \
  --certificate-arn $CERT_ARN \
  --query 'Certificate.Status'

# Get Validation Record (if needed)
aws acm describe-certificate \
  --certificate-arn $CERT_ARN \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord'
```

### DNS Records Summary

After installation, you need to create:

**For Certificate Validation (if auto):**
```
Type: CNAME
Name: _abc123.yourdomain.com
Value: _xyz789.acm-validations.aws.
TTL: 300
```

**For Application Access:**
```
Type: CNAME
Name: chat.yourdomain.com
Value: k8s-dial-xxx.il-central-1.elb.amazonaws.com
TTL: 300

Type: CNAME
Name: admin.yourdomain.com
Value: k8s-dial-xxx.il-central-1.elb.amazonaws.com
TTL: 300

Type: CNAME
Name: core.yourdomain.com
Value: k8s-dial-xxx.il-central-1.elb.amazonaws.com
TTL: 300

Type: CNAME
Name: themes.yourdomain.com
Value: k8s-dial-xxx.il-central-1.elb.amazonaws.com
TTL: 300
```

---

## 💡 Tips

1. **Lower TTL during setup** - Use TTL of 300 (5 minutes) while setting up, increase later
2. **Test with curl first** - Before testing in browser
3. **Clear browser cache** - If you see certificate errors
4. **Use incognito mode** - To avoid cache issues
5. **Check CloudWatch** - For ALB access logs and errors

---

## ✅ Success Checklist

- [ ] Certificate validated (if using auto)
- [ ] 4 CNAME records created (chat, admin, core, themes)
- [ ] DNS propagated (nslookup works)
- [ ] HTTPS works (curl returns 200)
- [ ] Can access chat.yourdomain.com in browser
- [ ] Can access admin.yourdomain.com in browser

---

## 🎯 Common Scenarios

### Scenario 1: Domain in GoDaddy, Using Auto Certificate

```bash
# 1. Install
bash install.sh

# 2. Get validation record
aws acm describe-certificate --certificate-arn <ARN> \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord'

# 3. Add CNAME in GoDaddy for validation

# 4. Wait for validation

# 5. Get ALB DNS
kubectl get ingress -n dial

# 6. Add 4 CNAMEs in GoDaddy pointing to ALB

# 7. Access DIAL
```

### Scenario 2: Domain in Cloudflare, Using Existing Cert

```bash
# 1. Import cert to ACM (if needed)
aws acm import-certificate --certificate fileb://cert.pem ...

# 2. Update parameters.conf with cert ARN

# 3. Install
bash install.sh

# 4. Get ALB DNS
kubectl get ingress -n dial

# 5. Add 4 CNAMEs in Cloudflare
# IMPORTANT: Set to "DNS only" (gray cloud)

# 6. Access DIAL
```

---

## 📞 Need Help?

- Check certificate status in AWS Console → Certificate Manager
- Check DNS propagation: https://www.whatsmydns.net/
- Check ALB health: EC2 Console → Load Balancers
- GitHub Issues: https://github.com/YOUR-ORG/dial-aws-installation/issues

**Remember: You do NOT need Route53. Any DNS provider works!**
