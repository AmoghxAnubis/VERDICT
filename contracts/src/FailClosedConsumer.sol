// SPDX-License-Identifier: UNLICENSED
pragma solidity 0.8.30;

import {DiagnosticTypes as Types} from "./DiagnosticTypes.sol";

interface IDiagnosticOracle {
    function latestDiagnostic() external view returns (
        Types.Report memory report, bytes32 digest, address signer, bool fresh
    );
}

/// @notice Example application policy. V1 diagnostics always leave usable reads disabled.
contract FailClosedConsumer {
    IDiagnosticOracle public immutable oracle;
    uint32 public immutable maximumAge;
    uint16 public immutable minimumConfidenceBps;

    error InvalidConsumerConfiguration();
    error FeedUnavailable();

    constructor(address source, uint32 age, uint16 confidenceBps) {
        if (source.code.length == 0 || age == 0 || age > 3600 || confidenceBps > 10000)
            revert InvalidConsumerConfiguration();
        oracle = IDiagnosticOracle(source);
        maximumAge = age;
        minimumConfidenceBps = confidenceBps;
    }

    function canAct() public view returns (bool) {
        (Types.Report memory report,,, bool fresh) = oracle.latestDiagnostic();
        return _usable(report, fresh);
    }

    function readPrice() external view returns (uint256 value, uint16 confidenceBps, uint64 observedAt) {
        (Types.Report memory report,,, bool fresh) = oracle.latestDiagnostic();
        if (!_usable(report, fresh)) revert FeedUnavailable();
        return (report.value, report.confidenceBps, report.observedAt);
    }

    function _usable(Types.Report memory report, bool fresh) private view returns (bool) {
        return fresh && report.status == 1 && report.publicationReady && report.value > 0
            && report.confidenceBps >= minimumConfidenceBps && report.confidenceBps <= 10000
            && report.decimals == 18 && block.timestamp >= report.observedAt
            && block.timestamp - report.observedAt <= maximumAge;
    }
}
