import { resolvePolymarketIdentityDisplay } from '../src/shared/utils/identity'

const assert = (condition: unknown, message: string): void => {
  if (!condition) {
    throw new Error(message)
  }
}

const profileDisplay = resolvePolymarketIdentityDisplay({
  wallet_address: '0x1111111111111111111111111111111111111111',
  funder_address: '0x2222222222222222222222222222222222222222',
  signature_type: 1,
  profile_address: '0x2222222222222222222222222222222222222222',
  profile_name: 'FDV Trader',
  profile_pseudonym: 'Poly-2222',
  profile_image: 'https://example.com/avatar.png',
  profile_verified: true,
  profile_x_username: 'fdv_trader',
})

assert(profileDisplay.title === 'FDV Trader', 'profile display should use the Polymarket username')
assert(profileDisplay.imageUrl === 'https://example.com/avatar.png', 'profile display should use the Polymarket avatar')
assert(profileDisplay.verified, 'profile display should expose verified profile state')
assert(
  profileDisplay.tradingAccountAddress === '0x2222222222222222222222222222222222222222',
  'profile display should expose the Polymarket trading account',
)
assert(
  profileDisplay.signingWalletAddress === '0x1111111111111111111111111111111111111111',
  'profile display should expose the signing wallet separately',
)

const pseudonymDisplay = resolvePolymarketIdentityDisplay({
  wallet_address: '0x1111111111111111111111111111111111111111',
  funder_address: '0x2222222222222222222222222222222222222222',
  signature_type: 1,
  profile_address: '0x2222222222222222222222222222222222222222',
  profile_name: null,
  profile_pseudonym: 'Poly-2222',
  profile_image: null,
  profile_verified: false,
  profile_x_username: null,
})

assert(pseudonymDisplay.title === 'Poly-2222', 'profile display should use pseudonym when no public username exists')
assert(pseudonymDisplay.imageUrl === null, 'profile display should not invent an avatar URL')
