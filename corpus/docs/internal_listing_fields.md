# openigloo Internal Listing Fields

<!-- doc_id: internal_listing_fields | issuer: openigloo | source_url: internal | effective_from: 2025-01-15 | effective_to: None | supersedes: None | superseded_by: None | visibility: staff | version: 1 -->

## Who this document is for

This document is for openigloo staff only. It describes fields on a listing record that are not shown to renters or landlords.

## voucher_friendly

A boolean set by the trust team, not by the landlord. True when the landlord has completed at least one voucher lease through openigloo or has been verified by staff to accept vouchers. A listing with voucher_friendly set to false is not hidden from voucher holders; the flag controls whether the assistant proactively mentions the listing to a renter who has said they hold a voucher. The flag is never disclosed to landlords or renters.

## fee_payer_declared

One of landlord, renter, or none. Set from the landlord's declaration at listing creation. A listing with fee_payer_declared equal to renter and no fee amount is held in moderation and not published.

## moderation_notes

Free text written by staff. May contain the reason a listing was held, prior complaints against the landlord, and the outcome of trust team reviews. Never shown outside the staff console.

## decline_count_voucher_60d

A rolling count of declined voucher-holder applications in the last 60 days. At 3 the listing is flagged for trust team review. See the Landlord Screening Guidelines.

## stabilization_hint

One of stabilized, likely_stabilized, unknown, or exempt. Derived from building registration data. It is a hint for staff and is not shown to renters, because it is wrong often enough that renters have relied on it and been overcharged. Renters are directed to DHCR to confirm stabilization status.
