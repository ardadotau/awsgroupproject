# INFS3701 — 12-month AWS cost estimate (official)

Source: **AWS Pricing Calculator** (`calculator.aws`) Export → PDF.

| Item | Value |
|------|--------|
| Region | US East (N. Virginia) `us-east-1` |
| Monthly | **104.83 USD** |
| 12 months | **1,257.96 USD** |
| Share URL | https://calculator.aws/#/estimate?id=b154a2ffd3a839596276e52c5dcb93c0f55dfe89 |
| PDF | `INFS3701_AWS_Pricing_Calculator_12_Month.pdf` (2 pages) |

## Included drivers (lean)

- Networking: 1× Regional NAT Gateway; 1× Application Load Balancer  
- Compute: 2× `t3.micro` Amazon Linux On-Demand; 2× 8 GB gp3 EBS  
- Database: Multi-AZ `db.t3.micro` RDS PostgreSQL, 20 GB gp3  
- Storage: 5 GB S3 Standard  

SNS/CloudWatch/etc. omitted as negligible vs NAT/ALB/RDS for this lab architecture.
