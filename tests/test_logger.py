from unittest.mock import MagicMock, patch
from custom_agents.strands_team import create_strands_agent

if __name__ == "__main__":
    config = {
        'team_id': 'test_team_123',
        'role': 'Test Agent',
        'goal': 'Testing',
        'backstory': 'A test agent',
        'tools': [],
        'named_agent_type': 'TestAgent',
    }
    with patch('custom_agents.strands_agent.Agent') as mock_agent_class:
        mock_agent_class.return_value = MagicMock()
        agent = create_strands_agent(config)
        print("Logger type:", type(agent._logger))
