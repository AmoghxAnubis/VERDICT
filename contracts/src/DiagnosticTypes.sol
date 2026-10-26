// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.30;

library DiagnosticTypes {
    struct Report {
        uint16 schemaVersion;
        bytes32 feedId;
        bytes32 datasetId;
        bytes32 assessmentId;
        bytes32 evidenceRoot;
        bytes32 configurationHash;
        bytes32 policyId;
        bytes32 keyId;
        uint64 roundId;
        uint64 observedAt;
        uint64 issuedAt;
        uint64 validUntil;
        uint256 value;
        uint16 confidenceBps;
        uint8 status;
        uint8 decimals;
        bool publicationReady;
    }

    struct Grant {
        bytes32 keyId;
        address signer;
        uint64 validFrom;
        uint64 validUntil;
        uint64 revokedAt;
        bool revoked;
    }
}
