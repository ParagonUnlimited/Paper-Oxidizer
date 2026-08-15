//! R2 presigned GETs. The app never streams image bytes -- it signs a URL and
//! redirects, so a JPEG travels R2 -> browser directly and the bucket stays
//! private (probate documents).

use crate::config::R2;
use anyhow::Result;
use aws_config::BehaviorVersion;
use aws_credential_types::Credentials;
use aws_sdk_s3::config::{RequestChecksumCalculation, ResponseChecksumValidation};
use aws_sdk_s3::presigning::PresigningConfig;
use std::time::Duration;

pub struct R2Client {
    client: aws_sdk_s3::Client,
    bucket: String,
    prefix: String,
    ttl: Duration,
}

impl R2Client {
    pub fn new(cfg: &R2) -> Self {
        let creds = Credentials::new(
            cfg.key_id.clone(), cfg.secret.clone(), None, None, "r2-env");
        let sdk = aws_sdk_s3::Config::builder()
            .behavior_version(BehaviorVersion::latest())
            .endpoint_url(&cfg.endpoint)
            .region(aws_sdk_s3::config::Region::new("auto"))
            .credentials_provider(creds)
            // R2 requires path-style addressing.
            .force_path_style(true)
            // The SDK's post-1.69 default (WhenSupported) adds checksum
            // headers R2's S3 compatibility rejects. WhenRequired restores
            // interoperability -- same break class that hit the JS SDK.
            .request_checksum_calculation(RequestChecksumCalculation::WhenRequired)
            .response_checksum_validation(ResponseChecksumValidation::WhenRequired)
            .build();
        R2Client {
            client: aws_sdk_s3::Client::from_conf(sdk),
            bucket: cfg.bucket.clone(),
            prefix: cfg.prefix.trim_matches('/').to_string(),
            ttl: Duration::from_secs(cfg.sign_ttl_secs),
        }
    }

    pub async fn page_url(&self, page_id: i64) -> Result<String> {
        let key = format!("{}/{}.jpg", self.prefix, page_id);
        let req = self.client.get_object()
            .bucket(&self.bucket)
            .key(key)
            .presigned(PresigningConfig::expires_in(self.ttl)?)
            .await?;
        Ok(req.uri().to_string())
    }
}
