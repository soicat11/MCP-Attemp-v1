import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import { Logger } from './logger';

export class MCPServerManager implements vscode.McpServerDefinitionProvider<vscode.McpStdioServerDefinition> {
  private logger: Logger;
  private extensionPath: string;

  constructor(logger: Logger, extensionPath: string) {
    this.logger = logger;
    this.extensionPath = extensionPath;
  }

  private getServerExecutablePath(): string {
    const platform = process.platform;
    let executableName: string;
    
    switch (platform) {
      case 'win32':
        executableName = 'powerbi-modeling-mcp.exe';
        break;
      case 'darwin':
        executableName = 'powerbi-modeling-mcp';
        break;
      default:
        this.logger.error(`Unsupported platform: ${platform}. Only Windows and macOS are supported.`);
        throw new Error(`Unsupported platform: ${platform}. Only Windows and macOS are supported.`);
    }
    
    const serverPath = path.join(this.extensionPath, 'server', executableName);
    this.logger.info(`Server path for platform ${platform}: ${serverPath}`);
    
    // Ensure execute permissions for macOS
    if (platform === 'darwin') {
      this.ensureExecutePermissions(serverPath);
    }
    
    return serverPath;
  }

  private ensureExecutePermissions(filePath: string): void {
    try {
      // Check if file exists
      if (!fs.existsSync(filePath)) {
        this.logger.info(`Executable file not found: ${filePath}`);
        return;
      }

      // Set execute permissions (0o755)
      fs.chmodSync(filePath, 0o755);
      
      this.logger.info(`Execute permissions set for macOS executable: ${filePath}`);
    } catch (error) {
      this.logger.error(`Failed to set execute permissions for ${filePath}: ${error}`);
    }
  }

  async provideMcpServerDefinitions(): Promise<vscode.McpStdioServerDefinition[]> {
    this.logger.info('Providing MCP server definitions');
    
    const serverPath = this.getServerExecutablePath();
    
    // Get configuration values
    const config = vscode.workspace.getConfiguration('');
    const argsString = config.get<string>('args', '--start');
    const envVarsString = config.get<string>('environmentVariables', '');
  
    
    // Parse arguments from string (simple split by spaces)
    const args = argsString.trim().split(/\s+/).filter(arg => arg.length > 0);
    
    // Parse environment variables
    const environmentVariables = this.parseEnvironmentVariables(envVarsString);
    
    this.logger.info(`MCP server args: ${args.join(' ')}`);
    if (Object.keys(environmentVariables).length > 0) {
      this.logger.info(`Environment variables: ${JSON.stringify(environmentVariables)}`);
    }
    
    return [
      new vscode.McpStdioServerDefinition(
        'powerbi-modeling-mcp',
        serverPath,
        args,
        Object.keys(environmentVariables).length > 0 ? environmentVariables : undefined,
        '1.0.0' // version
      )
    ];
  }

  private parseEnvironmentVariables(envVarsString: string): Record<string, string> {
    const envVars: Record<string, string> = {};
    
    if (!envVarsString.trim()) {
      return envVars;
    }
    
    // Split by commas and parse key=value pairs
    const parts = envVarsString.split(',');
    
    for (const part of parts) {
      const trimmedPart = part.trim();
      if (trimmedPart) {
        const equalIndex = trimmedPart.indexOf('=');
        if (equalIndex > 0) {
          const key = trimmedPart.substring(0, equalIndex).trim();
          const value = trimmedPart.substring(equalIndex + 1).trim();
          envVars[key] = value;
        }
      }
    }
    
    return envVars;
  }

  async resolveMcpServerDefinition(
    server: vscode.McpStdioServerDefinition
  ): Promise<vscode.McpStdioServerDefinition> {
    this.logger.info(`Resolving MCP server: ${server.label}`);
    return server;
  }
}