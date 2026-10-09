//! Deterministic PII recognisers, replacing `main`'s Presidio masking.
//!
//! NOT EQUIVALENT TO PRESIDIO, and not presented as such. Presidio combines
//! pattern recognisers with context words, confidence scores and a spaCy NER
//! model; this module has only the pattern half, tightened with checksums
//! where the entity has one (Luhn for cards, mod-97 for IBANs, Base58Check
//! and Bech32 for Bitcoin addresses). The trade, stated in docs/LIMITATIONS.md:
//!
//! * fewer false positives for checksummed entities, more false negatives for
//!   free-form ones (phone numbers in unusual layouts, international formats);
//! * no model, so it runs in microseconds inside the streaming window instead of
//!   forcing the whole answer to be buffered first;
//! * the same entity list and the same `<ENTITY_TYPE>` replacement as `main`.

use std::{net::Ipv6Addr, sync::LazyLock};

use regex::Regex;
use sha2::{Digest, Sha256};

/// The entity names `main`'s configuration uses.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Entity {
    EmailAddress,
    PhoneNumber,
    CreditCard,
    IbanCode,
    IpAddress,
    Crypto,
    UsSsn,
}

impl Entity {
    pub fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "EMAIL_ADDRESS" => Self::EmailAddress,
            "PHONE_NUMBER" => Self::PhoneNumber,
            "CREDIT_CARD" => Self::CreditCard,
            "IBAN_CODE" => Self::IbanCode,
            "IP_ADDRESS" => Self::IpAddress,
            "CRYPTO" => Self::Crypto,
            "US_SSN" => Self::UsSsn,
            _ => return None,
        })
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::EmailAddress => "EMAIL_ADDRESS",
            Self::PhoneNumber => "PHONE_NUMBER",
            Self::CreditCard => "CREDIT_CARD",
            Self::IbanCode => "IBAN_CODE",
            Self::IpAddress => "IP_ADDRESS",
            Self::Crypto => "CRYPTO",
            Self::UsSsn => "US_SSN",
        }
    }

    /// Presidio's replacement token.
    pub fn mask(self) -> String {
        format!("<{}>", self.name())
    }
}

/// One recognised entity: a byte range of the scanned text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Found {
    pub entity: Entity,
    pub start: usize,
    pub end: usize,
}

macro_rules! regex {
    ($pattern:expr) => {{
        static RE: LazyLock<Regex> = LazyLock::new(|| Regex::new($pattern).expect("PII pattern compiles"));
        &*RE
    }};
}

fn email() -> &'static Regex {
    regex!(
        r"(?i)\b[a-z0-9][a-z0-9._%+-]*@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,24}\b"
    )
}

fn phone() -> &'static Regex {
    // International with a leading +, or North American 3-3-4 with separators.
    regex!(r"(?:\+\d{1,3}[\s.-]?)?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b|\+\d{1,3}(?:[\s.-]?\(?\d{1,4}\)?){2,5}\d\b")
}

fn card_candidate() -> &'static Regex {
    regex!(r"\b(?:\d[ -]?){12,18}\d\b")
}

fn iban_candidate() -> &'static Regex {
    regex!(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b")
}

fn ipv4() -> &'static Regex {
    regex!(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
}

fn ipv6_candidate() -> &'static Regex {
    regex!(r"(?i)(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}")
}

fn btc_candidate() -> &'static Regex {
    regex!(r"\b(?:bc1[ac-hj-np-z02-9]{11,71}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b")
}

fn ssn() -> &'static Regex {
    regex!(r"\b(\d{3})[- .](\d{2})[- .](\d{4})\b")
}

/// Every configured entity in `text`, non-overlapping, in order.
pub fn find(text: &str, entities: &[Entity]) -> Vec<Found> {
    let mut found = Vec::new();
    let mut push = |entity: Entity, start: usize, end: usize| found.push(Found { entity, start, end });

    for &entity in entities {
        match entity {
            Entity::EmailAddress => {
                for m in email().find_iter(text) {
                    push(entity, m.start(), m.end());
                }
            }
            Entity::PhoneNumber => {
                for m in phone().find_iter(text) {
                    let digits = m.as_str().chars().filter(char::is_ascii_digit).count();
                    if (10..=15).contains(&digits) {
                        push(entity, m.start(), m.end());
                    }
                }
            }
            Entity::CreditCard => {
                for m in card_candidate().find_iter(text) {
                    let digits: Vec<u32> = m.as_str().chars().filter_map(|c| c.to_digit(10)).collect();
                    if (13..=19).contains(&digits.len()) && luhn(&digits) {
                        push(entity, m.start(), m.end());
                    }
                }
            }
            Entity::IbanCode => {
                for m in iban_candidate().find_iter(text) {
                    if iban_valid(m.as_str()) {
                        push(entity, m.start(), m.end());
                    }
                }
            }
            Entity::IpAddress => {
                for m in ipv4().find_iter(text) {
                    if m.as_str().split('.').all(|octet| octet.parse::<u8>().is_ok()) {
                        push(entity, m.start(), m.end());
                    }
                }
                for m in ipv6_candidate().find_iter(text) {
                    if m.as_str().matches(':').count() >= 2 && m.as_str().parse::<Ipv6Addr>().is_ok() {
                        push(entity, m.start(), m.end());
                    }
                }
            }
            Entity::Crypto => {
                for m in btc_candidate().find_iter(text) {
                    if btc_valid(m.as_str()) {
                        push(entity, m.start(), m.end());
                    }
                }
            }
            Entity::UsSsn => {
                for caps in ssn().captures_iter(text) {
                    let (area, group, serial) = (&caps[1], &caps[2], &caps[3]);
                    let valid =
                        area != "000" && area != "666" && !area.starts_with('9') && group != "00" && serial != "0000";
                    if valid {
                        let whole = caps.get(0).expect("group 0 exists");
                        push(entity, whole.start(), whole.end());
                    }
                }
            }
        }
    }

    // Earliest first; on a tie the longest wins; overlaps are dropped.
    found.sort_by(|a, b| a.start.cmp(&b.start).then(b.end.cmp(&a.end)));
    let mut kept: Vec<Found> = Vec::with_capacity(found.len());
    for candidate in found {
        if kept.last().is_none_or(|last| candidate.start >= last.end) {
            kept.push(candidate);
        }
    }
    kept
}

/// Replace every entity fully inside `text` by its mask.
pub fn mask(text: &str, found: &[Found]) -> String {
    let mut out = String::with_capacity(text.len());
    let mut cursor = 0;
    for item in found {
        if item.start < cursor || item.end > text.len() {
            continue;
        }
        out.push_str(&text[cursor..item.start]);
        out.push_str(&item.entity.mask());
        cursor = item.end;
    }
    out.push_str(&text[cursor..]);
    out
}

fn luhn(digits: &[u32]) -> bool {
    let sum: u32 = digits
        .iter()
        .rev()
        .enumerate()
        .map(|(index, &digit)| {
            if !index.is_multiple_of(2) {
                let doubled = digit * 2;
                if doubled > 9 { doubled - 9 } else { doubled }
            } else {
                digit
            }
        })
        .sum();
    sum.is_multiple_of(10)
}

fn iban_valid(candidate: &str) -> bool {
    let compact: String = candidate.chars().filter(|c| !c.is_whitespace()).collect();
    if !(15..=34).contains(&compact.len()) {
        return false;
    }
    let rearranged = format!("{}{}", &compact[4..], &compact[..4]);
    let mut remainder: u64 = 0;
    for c in rearranged.chars() {
        let value = match c {
            '0'..='9' => c as u64 - '0' as u64,
            'A'..='Z' => c as u64 - 'A' as u64 + 10,
            _ => return false,
        };
        let width = if value >= 10 { 100 } else { 10 };
        remainder = (remainder * width + value) % 97;
    }
    remainder == 1
}

fn btc_valid(candidate: &str) -> bool {
    if candidate.to_ascii_lowercase().starts_with("bc1") {
        bech32_valid(&candidate.to_ascii_lowercase())
    } else {
        base58check_valid(candidate)
    }
}

fn base58check_valid(candidate: &str) -> bool {
    const ALPHABET: &[u8] = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
    let mut bytes: Vec<u8> = Vec::new();
    for c in candidate.bytes() {
        let Some(mut carry) = ALPHABET.iter().position(|&a| a == c).map(|p| p as u32) else {
            return false;
        };
        for byte in bytes.iter_mut().rev() {
            carry += u32::from(*byte) * 58;
            *byte = (carry & 0xff) as u8;
            carry >>= 8;
        }
        while carry > 0 {
            bytes.insert(0, (carry & 0xff) as u8);
            carry >>= 8;
        }
    }
    let leading = candidate.bytes().take_while(|&b| b == b'1').count();
    let mut decoded = vec![0u8; leading];
    decoded.extend(bytes);
    if decoded.len() != 25 {
        return false;
    }
    let (payload, checksum) = decoded.split_at(21);
    let digest = Sha256::digest(Sha256::digest(payload));
    digest[..4] == *checksum
}

fn bech32_valid(candidate: &str) -> bool {
    const CHARSET: &[u8] = b"qpzry9x8gf2tvdw0s3jn54khce6mua7l";
    let Some((hrp, data)) = candidate.rsplit_once('1') else { return false };
    if hrp != "bc" || data.len() < 6 {
        return false;
    }
    let Some(values) =
        data.bytes().map(|c| CHARSET.iter().position(|&x| x == c).map(|p| p as u32)).collect::<Option<Vec<u32>>>()
    else {
        return false;
    };
    let mut checked: Vec<u32> = hrp.bytes().map(|b| u32::from(b) >> 5).collect();
    checked.push(0);
    checked.extend(hrp.bytes().map(|b| u32::from(b) & 31));
    checked.extend(values);
    let polymod = checked.iter().fold(1u32, |chk, &value| {
        let top = chk >> 25;
        let mut chk = ((chk & 0x1ff_ffff) << 5) ^ value;
        for (i, generator) in [0x3b6a_57b2u32, 0x2650_8e6d, 0x1ea1_19fa, 0x3d42_33dd, 0x2a14_62b3].iter().enumerate() {
            if (top >> i) & 1 == 1 {
                chk ^= generator;
            }
        }
        chk
    });
    // Bech32 (witness v0) or Bech32m (v1+).
    polymod == 1 || polymod == 0x2bc8_30a3
}

#[cfg(test)]
mod tests {
    use super::*;

    const ALL: [Entity; 7] = [
        Entity::EmailAddress,
        Entity::PhoneNumber,
        Entity::CreditCard,
        Entity::IbanCode,
        Entity::IpAddress,
        Entity::Crypto,
        Entity::UsSsn,
    ];

    fn masked(text: &str) -> String {
        mask(text, &find(text, &ALL))
    }

    #[test]
    fn each_entity_is_masked_with_presidios_token() {
        assert_eq!(masked("Mail renee.castillo@example.com today"), "Mail <EMAIL_ADDRESS> today");
        assert_eq!(masked("Call (415) 555-0132 now"), "Call <PHONE_NUMBER> now");
        assert_eq!(masked("Call +44 20 7946 0958 now"), "Call <PHONE_NUMBER> now");
        assert_eq!(masked("Card 4111 1111 1111 1111 on file"), "Card <CREDIT_CARD> on file");
        assert_eq!(masked("IBAN GB82 WEST 1234 5698 7654 32 ok"), "IBAN <IBAN_CODE> ok");
        assert_eq!(masked("From 192.168.10.24 and 2001:db8::8a2e:370:7334"), "From <IP_ADDRESS> and <IP_ADDRESS>");
        assert_eq!(masked("Pay 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2 now"), "Pay <CRYPTO> now");
        assert_eq!(masked("Pay bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq now"), "Pay <CRYPTO> now");
        assert_eq!(masked("SSN 078-05-1120."), "SSN <US_SSN>.");
    }

    #[test]
    fn the_repository_guardrail_fixture_is_fully_masked() {
        // db/guardrail_test_fixtures.sql, TKT-GR-PII, verbatim.
        let fixture =
            "Synthetic test contact: alice.guardrail@example.com, phone +41 44 668 18 00, IBAN DE89370400440532013000.";
        assert_eq!(masked(fixture), "Synthetic test contact: <EMAIL_ADDRESS>, phone <PHONE_NUMBER>, IBAN <IBAN_CODE>.");
    }

    #[test]
    fn checksums_reject_lookalikes() {
        assert_eq!(masked("Card 4111 1111 1111 1112"), "Card 4111 1111 1111 1112");
        assert_eq!(masked("IBAN GB82 WEST 1234 5698 7654 33"), "IBAN GB82 WEST 1234 5698 7654 33");
        assert_eq!(masked("addr 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3"), "addr 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3");
        assert_eq!(masked("SSN 000-12-3456"), "SSN 000-12-3456");
        assert_eq!(masked("IP 999.1.1.1"), "IP 999.1.1.1");
    }

    #[test]
    fn ticket_data_is_left_alone() {
        // The seed data's identifiers, timestamps, money and names must
        // survive: masking them would destroy the answer.
        for text in [
            "TKT-1001 for order ORD-5510 created 2026-09-15T09:20:00Z by Renee Castillo",
            "Refund of $4,250.75 approved; event EVT-1003 at 14:05:00",
            "Assigned to Priya Shah, priority high, 3 history events",
        ] {
            assert_eq!(masked(text), text);
        }
    }

    #[test]
    fn only_configured_entities_are_masked() {
        let text = "renee@example.com from 10.0.0.1";
        assert_eq!(mask(text, &find(text, &[Entity::IpAddress])), "renee@example.com from <IP_ADDRESS>");
    }

    #[test]
    fn unknown_entity_names_are_not_silently_accepted() {
        assert!(Entity::parse("PERSON").is_none());
        assert!(Entity::parse("email_address").is_none());
        assert_eq!(Entity::parse("US_SSN"), Some(Entity::UsSsn));
    }
}
