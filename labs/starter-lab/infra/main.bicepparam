using './main.bicep'

param environmentName = readEnvironmentVariable('AZURE_ENV_NAME', 'srelab')
param location = readEnvironmentVariable('AZURE_LOCATION', 'eastus2')
param existingAgentResourceId = readEnvironmentVariable('EXISTING_SRE_AGENT_RESOURCE_ID', '')
