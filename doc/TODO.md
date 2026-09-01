# Preparation_1
- [ ] Find a valid raw database
- [ ] Find a clear way to count tokens (for both input and output operations)
- [ ] Find a good model and prompt for serialization
- [ ] Create a GraphDB

# Test_1: Fill the GraphDB
- [ ] Find out how many tokens are required to serialize the nodes from raw data
- [ ] Find out problems on the serialized data
- [ ] Evaluate costs with real money
- [ ] Find out how many time it takes to get a valid output

# Preparation_2
- [ ] Create an MCP server
- [ ] Find a good model to use MCP for retrival operations as an agent
- [ ] Config MCP clients

# Test_2: Test Agentic Retrival
Use the *same prompt* for 3 different approaches:
1. Agent + MCP + GraphRAG
2. Agent + Document-serialized (.json/.md) database 
3. Agent + Raw database
Retrive all infos about: 
- costs
- elaboration times
- token balance