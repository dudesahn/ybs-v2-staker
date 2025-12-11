// SPDX-License-Identifier: AGPL-3.0
pragma solidity ^0.8.18;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {SafeERC20, IERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ICurve} from "contracts/interfaces/curve/ICurve.sol";
import {ICurveInt128} from "contracts/interfaces/curve/ICurveInt128.sol";

interface IZap {
    function zap(
        address _inputToken,
        address _outputToken,
        uint256 _amountIn,
        uint256 _minOut,
        address _recipient
    ) external returns (uint256);
}

interface IVault is IERC20 {
    struct StrategyParams {
        uint256 performanceFee;
        uint256 activation;
        uint256 debtRatio;
        uint256 minDebtPerHarvest;
        uint256 maxDebtPerHarvest;
        uint256 lastReport;
        uint256 totalDebt;
        uint256 totalGain;
        uint256 totalLoss;
    }

    function deposit(uint amount, address recipient) external returns (uint);

    function withdraw(uint shares, address recipient) external returns (uint);

    function strategies(address) external returns (StrategyParams memory);

    function asset() external returns (address);

    function token() external returns (address);
}

contract SwapperV5 {
    using SafeERC20 for ERC20;

    uint public constant PRECISION = 1e18;
    ERC20 public immutable tokenIn;
    ERC20 public immutable tokenOut;
    ERC20 public immutable tokenOutPool1;
    ICurve public immutable pool1; // 0xec977F46467a3021785Cff88894886E617abd65b, crvUSD-YB
    ICurveInt128 public immutable pool2; // not live yet
    uint public immutable pool1InTokenIdx; // 0
    uint public immutable pool1OutTokenIdx; // 1
    bool public otcEnabled;
    address public constant owner = 0xFEB4acf3df3cDEA7399794D0869ef76A6EfAff52;
    address public constant treasury =
        0x044F9C86a0Da637a235E83564215DC271Bc0deFc; // updated to use revenue recipient
    IZap public constant zap = IZap();
    IVault public vault = IVault(0xBF319dDC2Edc1Eb6FDf9910E39b37Be221C8805F); // yvcrvUSD-2
    IVault public constant approvedVault =
        IVault(); // add yvyYB address here
    address public management;
    mapping(address => bool) public allowedSwapper;
    mapping(address => bool) public operator;

    modifier isAllowedSwapper() {
        require(
            approvedVault.strategies(msg.sender).activation > 0 ||
                allowedSwapper[msg.sender],
            "!AllowedSwapper"
        );
        _;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "!owner");
        _;
    }

    modifier onlyOwnerOrManagement() {
        require(
            msg.sender == owner || msg.sender == management,
            "!ownerOrManagement"
        );
        _;
    }

    modifier onlyOperator() {
        require(
            msg.sender == owner ||
                msg.sender == management ||
                operator[msg.sender],
            "!operator"
        );
        _;
    }

    event OTC(uint price, uint sellTokenAmount, uint buyTokenAmount);
    event SetVault(address indexed vault);
    event SetAllowedSwapper(address indexed caller, bool indexed isAllowed);
    event SetOperator(address indexed caller, bool indexed isAllowed);
    event SetManagement(address indexed management);
    event OTCEnabled(bool indexed enabled);

    constructor(
        address _management,
        ERC20 _tokenIn,
        ERC20 _tokenOut,
        ICurve _pool1,
        ERC20 _tokenOutPool1,
        ICurveInt128 _pool2
    ) {
        tokenIn = _tokenIn;
        tokenOut = _tokenOut;
        management = _management;
        pool1 = _pool1;
        pool2 = _pool2;
        tokenOutPool1 = _tokenOutPool1;

        require(address(_tokenOut) == approvedVault.token(), "!token");

        uint idxFound;
        address token;

        for (uint i; i < 3; ++i) {
            token = _pool1.coins(i);
            if (token == address(_tokenIn)) {
                pool1InTokenIdx = i;
                idxFound++;
                if (idxFound == 2) break;
            }
            if (token == address(_tokenOutPool1)) {
                pool1OutTokenIdx = i;
                idxFound++;
                if (idxFound == 2) break;
            }
        }

        tokenIn.approve(address(_pool1), type(uint).max);
        tokenIn.approve(address(vault), type(uint).max);
        tokenOutPool1.approve(address(zap), type(uint).max);
    }

    function swap(uint _amount) external returns (uint profit) {
        tokenIn.safeTransferFrom(msg.sender, address(this), _amount);
        if (otcEnabled) (profit, _amount) = _sellOtc(_amount);
        if (_amount < PRECISION) return profit;
        uint out = pool1.exchange(
            pool1InTokenIdx,
            pool1OutTokenIdx,
            _amount,
            0
        );
        return
            profit += zap.zap(
                address(tokenOutPool1),
                address(tokenOut),
                out,
                0,
                msg.sender
            );
    }

    // Returns amount of profit and amount of sell tokens remaining to be sold.
    function _sellOtc(
        uint _sellTokenAmount
    ) internal isAllowedSwapper returns (uint, uint) {
        ERC20 buyToken = tokenOut;
        uint price = priceOracle();
        uint amountToSell = _sellTokenAmount;
        uint amountToBuy = (amountToSell * price) / PRECISION;
        uint buyTokenBalance = buyToken.balanceOf(address(this));
        if (amountToBuy > buyTokenBalance) {
            // check for vault tokens to withdraw from
            uint256 vaultBalance = approvedVault.balanceOf(address(this));
            if (vaultBalance > amountToBuy) {
                // this will actually withdraw ~2x the yCRV since PPS is around 2
                // note that since this is a V2 vault, input amount is shares, not underlying
                approvedVault.withdraw(amountToBuy, address(this));
            } else {
                if (vaultBalance > 0) {
                    approvedVault.withdraw(vaultBalance, address(this));
                    buyTokenBalance = buyToken.balanceOf(address(this));
                }
                if (amountToBuy > buyTokenBalance) {
                    amountToBuy = buyTokenBalance;
                    amountToSell = (PRECISION * buyTokenBalance) / price;
                }
            }
        }
        buyToken.safeTransfer(msg.sender, amountToBuy);
        vault.deposit(amountToSell, treasury);
        emit OTC(price, amountToSell, amountToBuy);
        return (amountToBuy, _sellTokenAmount - amountToSell);
    }
    
    /// @notice Returns the price of crvUSD to yYB
    /// @dev To get price of yYB in USD, do 1e18 / priceOracle()
    function priceOracle() public view returns (uint) {
        uint oraclePricePool1 = pool1.price_oracle();
        uint oraclePricePool2 = pool2.price_oracle(0);
        return 1e54 / (oraclePricePool1 * oraclePricePool2);
    }

    function sweep(address _token) external onlyOwnerOrManagement {
        uint amount = ERC20(_token).balanceOf(address(this));
        if (amount > 0) ERC20(_token).safeTransfer(owner, amount);
    }

    function enableOtc(bool _enabled) external onlyOperator {
        otcEnabled = _enabled;
        emit OTCEnabled(_enabled);
    }

    // Owner only function to switch the vault used to wrap the purchased asset before transferring to treasury
    function setVault(IVault _vault) external onlyOwner {
        require(_vault.asset() == address(tokenIn), "wrong asset");
        tokenIn.approve(address(vault), 0);
        tokenIn.approve(address(_vault), type(uint256).max);
        vault = _vault;
        emit SetVault(address(_vault));
    }

    // Permit a caller to OTC against funds in this contract
    function setAllowedSwapper(
        address _caller,
        bool _isAllowed
    ) external onlyOwnerOrManagement {
        allowedSwapper[_caller] = _isAllowed;
        emit SetAllowedSwapper(_caller, _isAllowed);
    }

    // Permit a caller to enable and disable OTC
    function setOperator(
        address _caller,
        bool _isAllowed
    ) external onlyOwnerOrManagement {
        operator[_caller] = _isAllowed;
        emit SetOperator(_caller, _isAllowed);
    }

    function setManagement(address _management) external onlyOwner {
        management = _management;
        emit SetManagement(_management);
    }
}
