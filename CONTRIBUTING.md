# Contributing to DIAL AWS Installation

Thank you for your interest in contributing!

## How to Report Issues

If you encounter problems with the installation:

1. Check the [README](README.md) troubleshooting section first
2. Verify your AWS account has necessary permissions
3. Open an issue with:
   - Your AWS region
   - Error messages (remove any sensitive information!)
   - Steps to reproduce
   - Your `parameters.conf` (remove passwords!)

## How to Submit Changes

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/improvement`)
3. Make your changes
4. Test the installation in your AWS account
5. Commit your changes (`git commit -am 'Add improvement'`)
6. Push to the branch (`git push origin feature/improvement`)
7. Open a Pull Request

## Testing Changes

Before submitting a PR:

1. Validate all CloudFormation templates:
   ```bash
   for template in cloudformation/*.yaml; do
     aws cloudformation validate-template --template-body file://$template
   done
   ```

2. Test the installation script:
   ```bash
   bash -n install.sh  # Syntax check
   ```

3. Perform a full installation in a test AWS account

## Code Style

- Use clear, descriptive variable names
- Add comments for complex logic
- Follow existing formatting conventions
- Keep CloudFormation templates organized and readable

## Security

- Never commit credentials, API keys, or passwords
- Review changes for potential security issues
- Report security vulnerabilities privately to [security-email]

Thank you for contributing!
