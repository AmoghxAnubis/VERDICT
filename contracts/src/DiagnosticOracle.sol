// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.30;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Ownable2Step} from "@openzeppelin/contracts/access/Ownable2Step.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {DiagnosticTypes as Types} from "./DiagnosticTypes.sol";

/// @notice Stores authenticated unavailable diagnostics. V1 cannot publish usable prices.
contract DiagnosticOracle is EIP712, Ownable2Step {
    bytes32 public constant FEED_ID = keccak256("ETH-USD");
    bytes32 public constant REPORT_TYPEHASH = keccak256(
        "DiagnosticReport(uint16 schemaVersion,bytes32 feedId,bytes32 datasetId,bytes32 assessmentId,bytes32 evidenceRoot,bytes32 configurationHash,bytes32 policyId,bytes32 keyId,uint64 roundId,uint64 observedAt,uint64 issuedAt,uint64 validUntil,uint256 value,uint16 confidenceBps,uint8 status,uint8 decimals,bool publicationReady)"
    );
    uint256 public constant MAX_KEYS = 128;
    bytes32 public immutable datasetId;

    bytes32 public policyId;
    bytes32 public configurationHash;
    bytes32 public registryId;
    uint64 public registryRevision;
    uint32 public maximumTtl;
    uint32 public maximumAge;
    uint64 public lastRound;
    bool public initialized;

    mapping(bytes32 => Types.Grant) public grants;
    mapping(bytes32 => bool) public usedRegistryIds;
    mapping(bytes32 => bool) public usedAssessmentIds;
    mapping(bytes32 => bool) public usedDigests;
    mapping(address => bytes32) private _addressKeys;
    mapping(bytes32 => uint64) private _seenRevision;
    bytes32[] private _keyIds;

    struct EmergencyRevocation { uint64 at; bool revoked; }
    mapping(bytes32 => EmergencyRevocation) public emergencyRevocations;

    Types.Report private _latest;
    bytes32 private _latestDigest;
    address private _latestSigner;

    error InvalidConfiguration();
    error AlreadyInitialized();
    error NotInitialized();
    error InvalidRegistry();
    error InvalidGrant();
    error AuthorizationExtended();
    error SignerInactive();
    error UnauthorizedSigner();
    error InvalidContext();
    error UnsupportedReport();
    error InvalidTimes();
    error ReportExpired();
    error ReplayedReport();
    error RenunciationDisabled();

    event RegistryApplied(bytes32 indexed previousId, bytes32 indexed registryId, uint64 revision);
    event SignerRevoked(bytes32 indexed keyId, uint64 at);
    event DiagnosticAccepted(
        bytes32 indexed assessmentId, bytes32 indexed evidenceRoot, bytes32 indexed digest,
        uint64 roundId, address signer, uint64 validUntil
    );

    constructor(address initialOwner, bytes32 expectedDatasetId)
        EIP712("VERDICT Diagnostic Reports", "1") Ownable(initialOwner)
    {
        if (expectedDatasetId == bytes32(0)) revert InvalidConfiguration();
        datasetId = expectedDatasetId;
    }

    /// @notice One-time configuration after the domain's actual deployment address is known.
    function initialize(
        bytes32 expectedPolicyId, bytes32 expectedConfigurationHash,
        uint32 ttl, uint32 age, bytes32 initialRegistryId, Types.Grant[] calldata snapshot
    ) external onlyOwner {
        if (initialized) revert AlreadyInitialized();
        if (expectedPolicyId == bytes32(0) || expectedConfigurationHash == bytes32(0)
            || ttl == 0 || ttl > 3600 || age == 0 || age > 3600) revert InvalidConfiguration();
        policyId = expectedPolicyId;
        configurationHash = expectedConfigurationHash;
        maximumTtl = ttl;
        maximumAge = age;
        _applyRegistry(bytes32(0), initialRegistryId, 1, snapshot);
        initialized = true;
    }

    /// @notice The administrator attests the Python registry hash and corresponding full snapshot.
    function applyRegistry(
        bytes32 expectedPreviousId, bytes32 nextRegistryId, uint64 revision,
        Types.Grant[] calldata snapshot
    ) external onlyOwner {
        if (!initialized) revert NotInitialized();
        _applyRegistry(expectedPreviousId, nextRegistryId, revision, snapshot);
    }

    function _applyRegistry(
        bytes32 expectedPreviousId, bytes32 nextRegistryId, uint64 revision,
        Types.Grant[] calldata snapshot
    ) private {
        if (expectedPreviousId != registryId || revision != registryRevision + 1
            || nextRegistryId == bytes32(0) || usedRegistryIds[nextRegistryId]
            || snapshot.length == 0 || snapshot.length > MAX_KEYS) revert InvalidRegistry();
        uint256 oldCount = _keyIds.length;
        bytes32 previousKey;
        for (uint256 i; i < snapshot.length; ++i) {
            Types.Grant calldata entry = snapshot[i];
            if (entry.keyId <= previousKey || entry.signer == address(0)
                || entry.validUntil <= entry.validFrom
                || (!entry.revoked && entry.revokedAt != 0)) revert InvalidGrant();
            previousKey = entry.keyId;
            Types.Grant storage old = grants[entry.keyId];
            if (old.signer != address(0)) {
                if (old.signer != entry.signer || old.validFrom != entry.validFrom)
                    revert InvalidGrant();
                if (entry.validUntil > old.validUntil || (old.revoked
                    && (!entry.revoked || entry.revokedAt > old.revokedAt)))
                    revert AuthorizationExtended();
            } else {
                if (_addressKeys[entry.signer] != bytes32(0)) revert InvalidGrant();
                _addressKeys[entry.signer] = entry.keyId;
                _keyIds.push(entry.keyId);
            }
            grants[entry.keyId] = entry;
            _seenRevision[entry.keyId] = revision;
        }
        for (uint256 i; i < oldCount; ++i) {
            if (_seenRevision[_keyIds[i]] != revision) revert InvalidRegistry();
        }
        usedRegistryIds[nextRegistryId] = true;
        registryId = nextRegistryId;
        registryRevision = revision;
        emit RegistryApplied(expectedPreviousId, nextRegistryId, revision);
    }

    /// @notice Immediate persistent kill switch, independent of registry distribution latency.
    function revokeSigner(bytes32 keyId) external onlyOwner {
        if (grants[keyId].signer == address(0)) revert InvalidGrant();
        EmergencyRevocation storage stop = emergencyRevocations[keyId];
        if (!stop.revoked) {
            stop.at = uint64(block.timestamp);
            stop.revoked = true;
            emit SignerRevoked(keyId, stop.at);
        }
    }

    function renounceOwnership() public pure override { revert RenunciationDisabled(); }

    function hashReport(Types.Report calldata report) public view returns (bytes32) {
        // All report fields are static ABI values, so this encodes typeHash then all 17 words.
        return _hashTypedDataV4(keccak256(abi.encode(REPORT_TYPEHASH, report)));
    }

    function submit(Types.Report calldata report, bytes calldata signature) external {
        if (!initialized) revert NotInitialized();
        if (report.feedId != FEED_ID || report.datasetId != datasetId
            || report.policyId != policyId || report.configurationHash != configurationHash
            || report.assessmentId == bytes32(0) || report.evidenceRoot == bytes32(0))
            revert InvalidContext();
        if (report.schemaVersion != 1 || report.decimals != 18 || report.status != 0
            || report.value != 0 || report.confidenceBps != 0 || report.publicationReady)
            revert UnsupportedReport();
        if (report.roundId == 0 || report.roundId != report.observedAt
            || report.observedAt > report.issuedAt || report.issuedAt >= report.validUntil
            || uint256(report.validUntil) > uint256(report.issuedAt) + maximumTtl
            || uint256(report.validUntil) > uint256(report.observedAt) + maximumAge)
            revert InvalidTimes();
        if (block.timestamp < report.issuedAt || block.timestamp >= report.validUntil)
            revert ReportExpired();
        Types.Grant storage grant = grants[report.keyId];
        if (!_authorized(grant, report)) revert SignerInactive();
        bytes32 digest = hashReport(report);
        address signer = ECDSA.recover(digest, signature);
        if (signer != grant.signer) revert UnauthorizedSigner();
        if (report.roundId <= lastRound || usedAssessmentIds[report.assessmentId]
            || usedDigests[digest]) revert ReplayedReport();
        usedAssessmentIds[report.assessmentId] = true;
        usedDigests[digest] = true;
        lastRound = report.roundId;
        _latest = report;
        _latestDigest = digest;
        _latestSigner = signer;
        emit DiagnosticAccepted(report.assessmentId, report.evidenceRoot, digest,
            report.roundId, signer, report.validUntil);
    }

    function _authorized(Types.Grant storage grant, Types.Report memory report) private view returns (bool) {
        return grant.signer != address(0) && report.issuedAt >= grant.validFrom
            && report.issuedAt < grant.validUntil && report.validUntil <= grant.validUntil
            && (!grant.revoked || block.timestamp < grant.revokedAt)
            && !emergencyRevocations[report.keyId].revoked;
    }

    /// @notice This is diagnostic metadata. Freshness does not imply usable output.
    function latestDiagnostic() external view returns (
        Types.Report memory report, bytes32 digest, address signer, bool fresh
    ) {
        report = _latest;
        digest = _latestDigest;
        signer = _latestSigner;
        fresh = lastRound != 0 && block.timestamp >= report.issuedAt
            && block.timestamp < report.validUntil && _authorized(grants[report.keyId], report);
    }
}
