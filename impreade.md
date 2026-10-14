The best method is a discovery-first, dependency-aware audit—not running hundreds of service scripts and not relying on billing alone.
AWS MCP can help execute and investigate, but it should be an interface over a deterministic audit workflow, not the source of truth.
Recommended audit architecture
Phase Purpose AWS evidence

1. Authenticate Confirm the client/account and permissions STS
2. Discover Identify resources, services, and relevant Regions AWS Config or Resource Explorer
3. Corroborate Add services recently billed or used Cost Explorer and CloudTrail
4. Map dependencies Connect primary resources to secondary/shared resources AWS Config relationships plus service APIs
5. Plan Generate only applicable security checks Local rule registry
6. Audit Run service-specific checks only where required RDS, S3, VPC, KMS, IAM APIs, etc.
7. Report gaps Distinguish absent, denied, unindexed, and unrecorded Evidence status
   The key rule is:
   “Not billed” does not mean “not present,” and “not discovered” does not mean “absent.”
   Free-tier, stopped, idle, newly created, shared, or zero-cost resources might not appear in billing data.
8. Client access model
   Avoid accepting permanent client keys whenever possible.
   The preferred model is:
   Your organization has an AWS auditor account.
   Each client creates a cross-account AuditReadOnlyRole.
   The trust policy permits your auditor account to assume it.
   Use a unique external ID for each client.
   AWS STS supplies temporary credentials.
   AWS recommends temporary credentials and cross-account roles for third-party access. The AWS CLI supports role profiles and external IDs directly. AWS CLI cross-account role documentation
   The client role can start with AWS’s SecurityAudit managed policy, which is intended for software that audits account security configuration, plus tightly scoped discovery and billing permissions. AWS SecurityAudit managed policy
   Add only the required permissions, for example:
   sts:GetCallerIdentity
   account:ListRegions
   AWS Config read/query operations
   Resource Explorer read/search operations
   ce:GetCostAndUsage and ce:GetDimensionValues
   tag:GetResources
   Required List, Describe, and configuration-related Get operations
   Explicitly exclude data access unless separately authorized:
   s3:GetObject
   secretsmanager:GetSecretValue
   ssm:GetParameter with decryption
   kms:Decrypt
   ecr:GetAuthorizationToken
   RDS Data API operations
   Application/database login operations
   If access key and secret key are the only available option, create a different named profile for every client:
   aws configure --profile client-acme-audit
   aws sts get-caller-identity --profile client-acme-audit
   Enter the secret interactively. Never place it in:
   the audit JSON configuration;
   scripts or source control;
   chat/email;
   command-line arguments;
   generated reports.
   Root-user access keys should never be used.
9. Use three different discovery signals
   No single AWS API answers “which AWS services are activated?”
   A. AWS Config — preferred when already enabled
   AWS Config is the strongest existing source because it can provide:
   current resource configuration;
   resource types and Regions;
   relationships between resources;
   historical configuration;
   multi-account/multi-Region aggregation;
   advanced SQL-like queries.
   An AWS Config aggregator provides a read-only centralized view and does not grant mutation access to source accounts. AWS Config advanced queries, AWS Config aggregation
   Important limitations:
   It sees only recorded/supported resource types.
   A disabled or incomplete recorder creates coverage gaps.
   Do not automatically enable AWS Config during an audit. That changes client infrastructure and may create costs.
   If AWS Config is already correctly configured, use it as the primary inventory and relationship source.
   B. AWS Resource Explorer — preferred lightweight discovery fallback
   Resource Explorer can search indexed resources across Regions from one aggregator Region. This avoids calling every individual service API merely to find out whether resources exist. AWS Resource Explorer cross-Region search
   Limitations:
   Only indexed Regions are covered.
   Resource-type coverage is not universal.
   It provides resource discovery and metadata, not every security configuration.
   Enabling indexes or an aggregator is a client-side configuration change. Check whether it already exists before using or proposing setup.
   Use Resource Explorer to produce:
   Resource type → Region → ARN/ID → owning service
   Then call only the detected service APIs.
   C. Cost Explorer — corroboration, not authoritative inventory
   Query the previous 60–90 days grouped by:
   SERVICE
   REGION
   Cost Explorer can identify services that incurred cost or usage, including resources that were recently deleted. Its API requires explicit permissions. AWS Cost Explorer API
   Use billing to answer:
   What generated cost?
   In which Regions?
   Are there old/deleted resources producing historical cost?
   Are there support, Marketplace, transfer, snapshot, or storage charges without an obvious live resource?
   Do not use it alone because it may miss free or idle resources. Resource-level cost data also requires additional opt-in configuration. Cost and usage with resources
10. Minimize Region discovery
    Use this order:
    Query enabled/opted-in Regions once.
    Read existing AWS Config aggregators.
    Read existing Resource Explorer indexes/views.
    Query Cost Explorer for billed Regions.
    Form a candidate Region set from their union.
    Perform service-specific inventory only in candidate Regions.
    If neither Config nor Resource Explorer is available, one lightweight discovery call per enabled Region may be unavoidable. That is still much better than running hundreds of service-specific checks in every Region.
    The Resource Groups Tagging API can supplement this process, but it returns resources that are currently or were previously tagged. It must not be treated as complete inventory because never-tagged and unsupported resources can be absent. Resource Groups Tagging GetResources
11. Primary and secondary classification
    For each discovered asset, classify it as:
    Primary workload: RDS instance, S3 bucket, EC2 instance, Lambda function.
    Direct dependency: subnet, security group, KMS key, IAM role.
    Operational dependency: CloudWatch alarm/log group, SNS topic, EventBridge rule, Backup plan.
    Security control: CloudTrail, Config recorder, GuardDuty, Security Hub, IAM Access Analyzer.
    Shared/global: IAM, Route 53, CloudFront, Organizations.
    Billed-only: cost exists but no live resource was found.
    Unknown: access denied, unindexed Region, or unrecorded resource type.
    For an account containing only RDS and S3, the generated plan should resemble:
    Primary:
    RDS
    S3
    Secondary/shared:
    VPC
    Subnets
    Route tables
    Network ACLs
    Security groups
    KMS
    IAM roles and policies
    Secrets Manager
    CloudWatch
    AWS Backup
    CloudTrail
    AWS Config
    Skipped unless independently detected:
    ECS
    EKS
    Lambda
    Redshift
    OpenSearch
    API Gateway
    ElastiCache
    DynamoDB
12. Targeted security checks
    For RDS, inspect only the discovered instances/clusters and their dependencies:
    public accessibility;
    encryption and KMS key;
    backup retention and automated backups;
    deletion protection;
    Multi-AZ;
    engine version and maintenance configuration;
    IAM database authentication;
    log exports;
    parameter and option groups;
    subnet group;
    VPC and security-group exposure;
    publicly routed subnets;
    snapshots and snapshot sharing;
    Secrets Manager integration and rotation;
    monitoring and alarms.
    For S3:
    bucket Region;
    account-level and bucket-level public-access blocks;
    policy and policy status;
    ACL and object ownership;
    default encryption and KMS key;
    versioning;
    Object Lock;
    access logging;
    CloudTrail data-event coverage;
    lifecycle;
    replication;
    CORS and website hosting;
    notification destinations;
    inventory configuration;
    bucket tags and ownership.
    CloudTrail Event History is useful only for the last 90 days of regional management events. It does not include S3 object-level data events; those require a trail or event data store configured for data events. CloudTrail Event History limitations, S3 data-event logging
    Could AWS MCP help?
    Yes—but use it carefully.
    The official AWS API MCP Server can:
    suggest valid AWS CLI commands;
    execute AWS APIs;
    validate commands;
    run in read-only mode;
    apply an MCP security policy;
    help with ad hoc investigation.
    However, AWS explicitly states that:
    IAM is still the primary security boundary;
    some read operations can expose credentials or sensitive information;
    the server is intended for a single user;
    it is not designed as a multi-tenant server;
    it has local filesystem access according to its configured mode. AWS API MCP Server documentation
    For client audits:
    run one MCP instance per client;
    use one AWS profile and separate working directory per client;
    set READ_OPERATIONS_ONLY=true;
    use an MCP allowlist/security-policy file;
    use no-access or workspace-only local filesystem mode;
    never expose one client’s credentials or evidence to another instance;
    rely on IAM explicit restrictions even when MCP read-only mode is enabled.
    MCP should handle questions such as:
    “Show the security groups related to this RDS instance.”
    It should not autonomously decide to enumerate every service and Region.
    Best design for the existing workspace
    The existing currently executes a fixed list of 49 checks. It is useful as an executor, but discovery and planning should be placed in front of it.
    I recommend evolving it into three modes:
    discover
    → inventory.json
    → dependency_map.json
    plan
    → audit_plan.json
    → skipped_checks.json with reasons
    audit
    → targeted service evidence
    → findings
    → coverage and permission gaps
    The report should show not only findings, but also:
    why each service was selected;
    why each service was skipped;
    inventory source;
    last billed date;
    affected Region;
    relationship path;
    permissions denied;
    confidence and coverage limitations.
    That gives you a defensible client audit without running 800+ scripts indiscriminately.
