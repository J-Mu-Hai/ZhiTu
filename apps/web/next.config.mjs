/** @type {import('next').NextConfig} */
const config = { reactStrictMode: true, devIndicators: false, distDir: process.env.NODE_ENV === 'development' ? '.next-dev' : '.next' };
export default config;
