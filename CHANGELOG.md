# DIAL AWS Installation - Changelog

## Version 1.1.0 (Updated)

### ✨ Major Changes

#### 1. Production-Grade Instance Types
- **Changed**: EKS nodes from `t3.large` to `m5.large`
- **Reason**: t-series not recommended for production workloads
- **Benefit**: Consistent performance, no CPU throttling
- **Cost Impact**: +~$20/month

#### 2. RDS Aurora Serverless v2
- **Changed**: From RDS PostgreSQL (`db.t3.medium`) to Aurora Serverless v2
- **Configuration**: 
  - Minimum capacity: 1 ACU
  - Maximum capacity: 128 ACU
  - Auto-scaling based on load
- **Benefits**:
  - Pay only for what you use
  - Auto-scaling (no manual intervention)
  - High availability (2 instances across AZs)
  - Better performance
- **Cost**: Variable, ~$50-150/month depending on usage

#### 3. Dual Cognito User Pools
- **Added**: Separate User Pools for Users and Admins
- **User Pool (Chat)**:
  - For end users accessing the Chat interface
  - Optional self-registration
  - MFA optional
  - Longer token validity
- **Admin Pool (Admin Portal)**:
  - For administrators only
  - Admin-only user creation
  - **MFA required** (enhanced security)
  - Shorter token validity (30 min vs 60 min)
  - Password min length: 12 chars (vs 8)
  - Custom role attribute
- **Benefit**: Separation of concerns, enhanced security for admins

#### 4. Default Region: Tel Aviv
- **Changed**: Default region from `us-east-2` to `il-central-1`
- **Benefit**: Lower latency for Israel-based users
- **Note**: Can still deploy to any AWS region

---

### 📋 Updated Configuration

#### New `parameters.conf` Defaults

```bash
# Region
AWS_REGION="il-central-1"  # Tel Aviv

# EKS Nodes
EKS_NODE_INSTANCE_TYPE="m5.large"  # Production-grade

# RDS Serverless (new parameters)
RDS_ENGINE="aurora-postgresql"
RDS_ENGINE_MODE="serverless"
RDS_MIN_CAPACITY="1"    # ACU
RDS_MAX_CAPACITY="128"  # ACU
```

#### Removed Parameters

```bash
# No longer needed (replaced by serverless)
# RDS_INSTANCE_TYPE
# RDS_STORAGE_GB
```

---

### 💰 Cost Impact

#### Monthly Cost Comparison

| Component | Previous | Updated | Change |
|-----------|----------|---------|--------|
| **EKS Nodes** | 3x t3.large (~$130) | 3x m5.large (~$155) | +$25 |
| **Database** | RDS t3.medium (~$60) | Aurora Serverless (~$50-150) | Variable |
| **Total** | ~$350-450 | ~$400-650 | +$50-200 |

**Note**: Aurora Serverless costs vary with usage. Light usage will be cheaper than fixed RDS, heavy usage may be more expensive but with better performance.

---

### 🔒 Security Enhancements

#### Admin User Pool
- ✅ **MFA Required** (was optional)
- ✅ **12-char minimum password** (was 8)
- ✅ **30-min token expiry** (was 60)
- ✅ **7-day refresh token** (was 30)
- ✅ **Admin-only creation** (always enforced)
- ✅ **Role attribute** for RBAC

#### Separation of Concerns
- ✅ Admin users isolated from regular users
- ✅ Separate authentication flows
- ✅ Different security policies per pool

---

### 📝 Migration Guide

#### From v1.0.0 to v1.1.0

If you already deployed v1.0.0:

**Option 1: Deploy New Stack (Recommended for Production)**
```bash
# Deploy alongside existing
STACK_NAME="dial-production-v2"
bash install.sh

# Migrate data
# Switch DNS
# Delete old stack
```

**Option 2: Update Existing Stack (Testing Only)**
```bash
# Update parameters.conf with new values
# Run install.sh
# Confirm update when prompted

# Note: This will cause downtime during DB migration
```

**Database Migration (Aurora)**:
1. Take RDS snapshot
2. CloudFormation creates new Aurora cluster
3. Restore data from snapshot manually
4. Update connection strings

---

### 🚀 What's Improved

#### Performance
- ✅ No CPU throttling (m5 instances)
- ✅ Auto-scaling database (Aurora Serverless)
- ✅ Multi-AZ database (2 instances)

#### Security
- ✅ Dual authentication pools
- ✅ Enforced MFA for admins
- ✅ Stronger admin passwords

#### Reliability
- ✅ Production-grade instances
- ✅ Aurora high availability
- ✅ Automatic database scaling

#### Cost
- ⚠️ Slightly higher base cost
- ✅ Aurora scales down when idle
- ✅ Pay for actual usage

---

### 📚 Updated Documentation

All documentation has been updated:
- ✅ README.md - Dual Cognito pools section
- ✅ QUICKSTART.md - Updated costs
- ✅ EXAMPLES.md - Tel Aviv region examples
- ✅ parameters.conf - New RDS parameters
- ✅ install.sh - Dual pool support
- ✅ create-admin-user.sh - Uses Admin pool

---

### 🔄 Breaking Changes

#### CloudFormation Outputs
**Added**:
- `AdminCognitoUserPoolId`
- `AdminCognitoClientId`
- `AdminCognitoClientSecret`
- `AdminCognitoIssuerUrl`
- `AdminCognitoJWKSUrl`

**Changed**:
- `CognitoUserPoolId` - Now refers to Chat pool only
- `CognitoClientId` - Now refers to Chat client only

#### Helm Values
**Admin section now uses separate Cognito**:
```yaml
dialadmin:
  frontend:
    env:
      AUTH_COGNITO_HOST: <ADMIN_POOL_HOST>  # Changed
      AUTH_COGNITO_CLIENT_ID: <ADMIN_CLIENT_ID>  # Changed
    secrets:
      AUTH_COGNITO_SECRET: <ADMIN_CLIENT_SECRET>  # Changed
```

---

### ✅ Compatibility

#### Backward Compatible
- ✅ VPC structure unchanged
- ✅ EKS configuration unchanged
- ✅ S3, Redis, IAM unchanged
- ✅ Parameters.conf mostly compatible

#### Not Backward Compatible
- ❌ Direct stack update (requires migration)
- ❌ RDS parameters changed completely
- ❌ Cognito outputs have new names

---

### 🐛 Bug Fixes

- Fixed region-specific service availability (Tel Aviv)
- Fixed Aurora Serverless v2 scaling configuration
- Improved error messages in install.sh
- Added validation for Aurora capacity values

---

### 📖 Additional Notes

#### Tel Aviv Region (il-central-1)
- Launched in 2023
- Full service availability including:
  - ✅ EKS
  - ✅ Aurora Serverless v2
  - ✅ ElastiCache Serverless
  - ✅ Cognito
  - ✅ All required services

#### Aurora Serverless v2 vs RDS
**Pros**:
- Auto-scaling
- Pay for actual usage
- Multi-AZ by default
- Better performance
- Zero-downtime scaling

**Cons**:
- Variable costs
- Minimum 1 ACU (~$0.12/hr)
- Learning curve for ACU sizing

#### m5 vs t3 Instances
**m5.large**:
- 2 vCPU, 8 GB RAM
- Consistent performance
- No CPU credits
- EBS-optimized
- **Best for**: Production workloads

**t3.large**:
- 2 vCPU, 8 GB RAM
- Burstable performance
- CPU credit system
- Lower baseline
- **Best for**: Dev/test

---

### 🎯 Recommendations

#### Production Deployments
1. ✅ Use m5.large nodes (default)
2. ✅ Set Aurora min capacity to 1 ACU
3. ✅ Set Aurora max capacity to 16-32 ACU for most workloads
4. ✅ Enable MFA for all admin users
5. ✅ Monitor Aurora scaling in CloudWatch

#### Cost Optimization
1. For light workloads, Aurora min=0.5 ACU saves money
2. For predictable loads, consider fixed RDS instead
3. For dev/test, can use smaller m5 instances

#### Security
1. Enforce MFA in both Cognito pools (if possible)
2. Regularly rotate database passwords
3. Use AWS Secrets Manager for credentials
4. Enable CloudTrail for audit logging

---

### 📞 Support

Questions about the update? Open an issue on GitHub:
https://github.com/YOUR-ORG/dial-aws-installation/issues

---

### 🙏 Credits

Thanks to community feedback for suggesting:
- Production-grade instance types
- Aurora Serverless for cost optimization
- Separate admin authentication
- Tel Aviv region support
