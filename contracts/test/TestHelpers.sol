// SPDX-License-Identifier: AGPL-3.0
pragma solidity 0.8.22;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

contract MockBaseFeeOracle {
    bool public acceptable;

    constructor(bool _acceptable) {
        acceptable = _acceptable;
    }

    function setAcceptable(bool _acceptable) external {
        acceptable = _acceptable;
    }

    function isCurrentBaseFeeAcceptable() external view returns (bool) {
        return acceptable;
    }
}

contract MockSwapper {
    IERC20 public immutable tokenIn;
    IERC20 public immutable tokenOut;

    constructor(IERC20 _tokenIn, IERC20 _tokenOut) {
        tokenIn = _tokenIn;
        tokenOut = _tokenOut;
    }

    function swap(uint256) external pure returns (uint256) {
        return 0;
    }
}
